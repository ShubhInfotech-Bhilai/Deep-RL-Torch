# External imports:
import itertools
import logging
import gym
import torch
from gym.spaces import Discrete

# Internal Imports:
from exp_rep import ReplayBuffer, PrioritizedReplayBuffer
from networks import Q, V, Actor, ProcessState, ProcessStateAction
from util import *
import copy


# This is the interface for the agent being trained by a Trainer instance
class AgentInterface:
    def __init__(self):
        pass

    def optimize(self):
        raise NotImplementedError

    def exploit(self, state):
        raise NotImplementedError

    def explore(self, state):
        raise NotImplementedError

    def update_targets(self, n_steps):
        raise NotImplementedError

    def decay_exploration(self, n_steps):
        raise NotImplementedError

    def calculate_TDE(self, state, action, reward, next_state):
        raise NotImplementedError

    def remember(self, state, action, next_state, reward, done):
        raise NotImplementedError

    def display_debug_info(self):
        raise NotImplementedError


class Agent(AgentInterface):
    def __init__(self, env, device, log, hyperparameters):
        self.discrete_env = True if "Discrete" in str(env.action_space)[:8] else False
        print(env.action_space)
        print("Env with discrete action space: ", self.discrete_env)
        self.env = env
        self.device = device
        self.log = log
        self.hyperparameters = hyperparameters

        self.use_actor_critic = hyperparameters["use_actor_critic"]

        self.F_s, self.F_sa = self.init_feature_extractors()
        self.policy = self.create_policy()

    def init_feature_extractors(self):
        F_s = ProcessState(self.env, self.log, self.device, self.hyperparameters)
        F_sa = None
        if self.use_actor_critic:
            state_feature_len = F_s.layers_merge[-1].out_features
            F_sa = ProcessStateAction(state_feature_len, self.env, self.log, self.device, self.hyperparameters)
        return F_s, F_sa

    def create_policy(self):
        # Define base policy of HRL or the general algorithm and ground policy of REM:
        if self.hyperparameters["use_actor_critic"]:
            base_policy = ActorCritic
        elif self.discrete_env:
            base_policy = Q_Policy
        else:
            raise NotImplementedError("The environment must be discrete to apply Q-Learning, no other"
                                      " framework than Actor-Critic available")
        if self.hyperparameters["use_REM"]:
            # The REM base policy creates an ensemble of ground_policies
            ground_policy = base_policy
            base_policy = REM
        else:
            ground_policy = None

        # Decide whether to use Hierarchical Reinforcement Learning:
        if self.hyperparameters["use_hrl"]:
            policy = HierarchicalPolicy(base_policy, ground_policy, self.log, self.hyperparameters)
        else:
            print("Base Policy (will act concretely): ", base_policy)
            print("Ground Policy (will use base policy): ", ground_policy)
            if self.hyperparameters["use_MineRL_policy"]:
                print("Use Hierarchical MineRL policy!")
                policy = MineRLHierarchicalPolicy(ground_policy, base_policy, self.F_s, self.F_sa, self.env,
                                                  self.device, self.log, self.hyperparameters)
            else:
                policy = base_policy(ground_policy, self.F_s, self.F_sa, self.env, self.device, self.log,
                                     self.hyperparameters)
        return policy

    def remember(self, state, action, next_state, reward, done):
        self.policy.remember(state, action, next_state, reward, done)

    def optimize(self):
        self.policy.optimize()

    def explore(self, state, fully_random=False):
        return self.policy.explore(state, fully_random)

    def exploit(self, state):
        return self.policy.exploit(state)

    def decay_exploration(self, n_steps):
        self.policy.decay_exploration(n_steps)

    def calculate_TDE(self, state, action, next_state, reward, done):
        return self.policy.calculate_TDE(state, action, next_state, reward, done)

    def update_targets(self, n_steps):
        self.policy.F_s.update_targets(n_steps)
        if self.policy.F_sa is not None:
            self.policy.F_sa.update_targets(n_steps)
        self.policy.update_targets(n_steps)

    def display_debug_info(self):
        self.policy.display_debug_info()

    def freeze_normalizers(self):
        self.policy.freeze_normalizers()


class BasePolicy:
    def __init__(self, ground_policy, F_s, F_sa, env, device, log, hyperparameters):
        self.env = env
        self.device = device
        self.log = log
        self.hyperparameters = hyperparameters
        self.ground_policy = ground_policy

        # Check env:
        self.discrete_env = True if 'Discrete' in str(env.action_space) else False
        if self.discrete_env:
            self.num_actions = self.env.action_space.n
            self.action_low = torch.zeros(self.num_actions, device=self.device)
            self.action_high = torch.ones(self.num_actions, device=self.device)
            print("Num actions: ", self.num_actions)
        else:
            self.num_actions = len(self.env.action_space.high)
            self.action_low = torch.tensor(env.action_space.low, device=self.device)
            self.action_high = torch.tensor(env.action_space.high, device=self.device)
            print("Env action low: ", self.action_low)
            print("Env action high: ", self.action_high)

        # Set up parameters:
        # Actor-Critic:
        self.use_actor_critic = hyperparameters["use_actor_critic"]
        self.use_CACLA_V = hyperparameters["use_CACLA_V"]
        self.use_CACLA_Q = hyperparameters["use_CACLA_Q"]
        self.use_DDPG = hyperparameters["use_DDPG"]
        self.use_SPG = hyperparameters["use_SPG"]
        self.use_GISPG = hyperparameters["use_GISPG"]
        # QV:
        self.use_QV = hyperparameters["use_QV"]
        self.use_QVMAX = hyperparameters["use_QVMAX"]
        # Exploration:
        self.gaussian_action_noise = hyperparameters["action_sigma"]
        self.boltzmann_exploration_temp = hyperparameters["boltzmann_temp"]
        self.epsilon = hyperparameters["epsilon"]
        self.eps_decay = hyperparameters["epsilon_decay"]
        # General:
        self.batch_size = hyperparameters["batch_size"]
        self.normalize_observations = hyperparameters["normalize_obs"]
        self.use_world_model = hyperparameters["use_world_model"]

        # TODO: -Include PER with prioritization based on Upper Bound of Gradient Norm.
        # TODO: -include different sampling schemes from the papers investigatin PER in SL (small and big buffer for gradient norm too)

        # TODO: -add goal to replay buffer and Transition (For HRL)
        # TODO: -add eligibility traces to replay buffer (probably the one that update after the current episode is done and after k steps)
        # Set up replay buffer:
        self.buffer_size = hyperparameters["replay_buffer_size"] + hyperparameters["num_expert_samples"]
        self.use_PER = hyperparameters["use_PER"]
        self.use_CER = hyperparameters["use_CER"]
        self.PER_alpha = hyperparameters["PER_alpha"]
        self.PER_beta = hyperparameters["PER_beta"]
        self.importance_weights = None
        # TODO: implement the option to linearly increase PER_beta to 1 over training time
        if self.use_PER:
            self.memory = PrioritizedReplayBuffer(self.buffer_size, self.PER_alpha, use_CER=self.use_CER)
        else:
            self.memory = ReplayBuffer(self.buffer_size, use_CER=self.use_CER)

        # Feature extractors:
        self.F_s = F_s
        self.F_sa = F_sa
        self.state_feature_len = F_s.layers_merge[-1].out_features
        if F_sa is not None:
            self.state_action_feature_len = F_sa.layers_merge[-1].out_features

        # Set up Networks:
        self.actor, self.Q, self.V = self.init_actor_critic(self.F_s, self.F_sa)

    def remember(self, state, action, reward, next_state, done):
        self.memory.add(state, action, reward, next_state, done)

    def random_action(self):
        action = (self.action_high - self.action_low) * torch.rand(self.num_actions, device=self.device,
                                                                   dtype=torch.float).unsqueeze(0) + self.action_low
        return action

    def boltzmann_exploration(self, action):
        pass

    # TODO: implement

    def explore_discrete_actions(self, action):
        if self.boltzmann_exploration_temp > 0:
            action = self.boltzmann_exploration(action)
        else:
            action = torch.argmax(action).item()
        return action

    def add_noise(self, action):
        if self.gaussian_action_noise:
            action += torch.tensor(np.random.normal(0, self.gaussian_action_noise, len(action)), dtype=torch.float)
            action = np.clip(action, self.action_low, self.action_high)
        return action

    def choose_action(self, state, calc_state_features=True):
        with torch.no_grad():
            if calc_state_features:
                state_features = self.F_s(state)
            else:
                state_features = state
            action = self.actor(state_features)
        return action

    def explore(self, state, fully_random=False):
        # Epsilon-Greedy:
        sample = random.random()
        if fully_random or sample < self.epsilon:
            raw_action = self.random_action()
        else:
            # Raw action:
            raw_action = self.choose_action(state)
            # Add Gaussian noise:
            if self.gaussian_action_noise:
                raw_action = self.add_noise(raw_action)

        # If env is discrete explore accordingly and set action
        if self.discrete_env:
            action = self.explore_discrete_actions(raw_action)
        else:
            action = raw_action[0].numpy()

        return action, raw_action

    def act(self, state):
        return self.explore(state)

    def exploit(self, state):
        raw_action = self.choose_action(state)
        if self.discrete_env:
            action = torch.argmax(raw_action).item()
        else:
            action = raw_action
        return action, raw_action

    def init_actor_critic(self, F_s, F_sa):
        Q_net, V_net = self.init_critic(F_s, F_sa)
        actor = self.init_actor(Q_net, V_net, F_s)
        # print(Q_net.actor)
        return actor, Q_net, V_net

    def init_critic(self, F_s, F_sa):
        # TODO: differentiate between DQN critic and AC critic by checking in Q __init__ for use_actor_critic

        if self.use_actor_critic:
            self.state_action_feature_len = F_sa.layers_merge[-1].out_features
            input_size = self.state_action_feature_len
        else:
            self.state_feature_len = F_s.layers_merge[-1].out_features
            input_size = self.state_feature_len

        Q_net = None
        if not (self.use_CACLA_V and not self.use_QVMAX):
            Q_net = Q(input_size, self.env, F_s, F_sa, self.device, self.log, self.hyperparameters)

        V_net = None
        if self.use_QV or self.use_QVMAX or (self.use_actor_critic and self.use_CACLA_V):
            # Init Networks:
            V_net = V(self.state_feature_len, self.env, F_s, None, self.device, self.log, self.hyperparameters)
        return Q_net, V_net

    def train_critic(self, Q, V, transitions, retain_graph=False):
        TDE_V = 0
        if self.V is not None:
            V.retain_graph = True
            TDE_V = V.optimize(transitions, transitions["PER_importance_weights"], self.actor, Q, None)

        # Only if we use standard CACLA (and we do not train the V net using QVMAX) we do not need a Q net:
        TDE_Q = 0
        if self.Q is not None:
            TDE_Q = Q.optimize(transitions, self.importance_weights, self.actor, None, V)

        TDE = (TDE_Q + TDE_V) / ((self.V is not None) + (self.Q is not None))

        TDE_abs = abs(TDE)
        return TDE_abs

    def optimize(self):
        # Get Batch:
        transitions = self.get_transitions()
        # Extract features:
        # TODO: we really need to convert a state_batch into vector/matrix stuff instead of hardcoding
        state_batch = transitions["state"]
        non_final_next_states = transitions["non_final_next_states"]
        state_feature_batch = self.F_s(state_batch)
        non_final_next_state_features = None
        if non_final_next_states is not None:
            non_final_next_state_features = self.F_s.forward_next_state(non_final_next_states)
        transitions["state_features"] = state_feature_batch
        transitions["non_final_next_state_features"] = non_final_next_state_features
        # Optimize:
        if self.use_world_model:
            self.world_model.optimize()
            # TODO: create a world model at some point
        error = self.optimize_networks(transitions)

        error = abs(error) + 0.0001
        error_np = error.cpu().detach().numpy()
        if self.use_PER:
            self.memory.update_priorities(transitions["PER_idxs"], error_np)

        self.display_debug_info()

    def decay_exploration(self, n_steps):
        if self.eps_decay:
            self.epsilon *= self.eps_decay
            self.log.add("Epsilon", self.epsilon)
        # TODO: decay temperature for Boltzmann if that exploration is used

    def get_transitions(self):
        sampling_size = min(len(self.memory), self.batch_size)
        if self.use_PER:
            transitions, importance_weights, PER_idxs = self.memory.sample(sampling_size, self.PER_beta)
            # print(importance_weights)
            importance_weights = torch.tensor(importance_weights, device=self.device).float()
        else:
            transitions = self.memory.sample(sampling_size)
            PER_idxs = None
            importance_weights = None
        # Transform the stored tuples into torch arrays:
        transitions = self.extract_batch(transitions)
        # Save PER relevant info:
        transitions["PER_idxs"] = PER_idxs
        transitions["importance_weights"] = importance_weights

        return transitions

    def extract_batch(self, transitions):
        # TODO: How fast is this? Can't we put it into arrays?
        # TODO: We sample, then concatenate the sampled parts into multiple arrays, transposing it before...

        # Transpose the batch (see http://stackoverflow.com/a/19343/3343043 for
        # detailed explanation).
        batch = Transition(*zip(*transitions))

        # Compute a mask of non-final states and concatenate the batch elements
        non_final_mask = torch.tensor(tuple(map(lambda s: s is not None,
                                                batch.next_state)), device=self.device, dtype=torch.bool)

        # Create state batch:
        if isinstance(batch.state[0], dict):
            # Concat the states per key:
            state_batch = {key: torch.cat([x[key] for x in batch.state]) for key in batch.state[0]}
        else:
            state_batch = torch.cat(batch.state)


        # print("non_final mask: ", non_final_mask)
        # print("state batch: ", state_batch)
        # print("next state batch: ", next_state_batch)
        # print("non final next state batch: ", non_final_next_states)

        # Create next state batch:
        non_final_next_states = [s for s in batch.next_state if s is not None]
        if non_final_next_states:
            if isinstance(non_final_next_states[0], dict):
                non_final_next_states = {key: torch.cat([x[key] for x in non_final_next_states]) for key in
                                         non_final_next_states[0]}
            else:
                non_final_next_states = torch.cat(non_final_next_states)
        else:
            non_final_next_states = None

        # Create action batch:
        #print(batch.action)
        action_batch = torch.cat(batch.action)

        # Create Reward batch:
        reward_batch = torch.cat(batch.reward).unsqueeze(1)

        transitions = {"state": state_batch, "action": action_batch, "reward": reward_batch,
                       "non_final_next_states": non_final_next_states, "non_final_mask": non_final_mask,
                       "state_action_features": None, "PER_importance_weights": None, "PER_idxs": None,
                       "action_argmax": torch.argmax(action_batch, 1).unsqueeze(1)}

        return transitions

    def optimize_networks(self, transitions):
        raise NotImplementedError

    def display_debug_info(self):
        pass

    def calculate_TDE(self, state, action, next_state, reward, done):
        return torch.tensor([0])
        # TODO fix
        return self.critic.calculate_TDE(state, action, next_state, reward, done)

    def set_retain_graph(self, val):
        if self.Q is not None:
            self.Q.retain_graph = val
        if self.V is not None:
            self.V.retain_graph = val
        if self.actor is not None:
            self.actor.retain_graph = val

    def update_targets(self, n_steps):
        if self.Q is not None:
            self.Q.update_targets(n_steps)
        if self.V is not None:
            self.V.update_targets(n_steps)
        if self.actor is not None and self.use_actor_critic:
            self.actor.update_targets(n_steps)
        if self.F_s is not None:
            self.F_s.update_targets(n_steps)
        if self.F_sa is not None:
            self.F_sa.update_targets(n_steps)

    def freeze_normalizers(self):
        self.F_s.freeze_normalizers()
        if self.F_sa is not None:
            self.F_sa.freeze_normalizers()

    def init_actor(self, Q, V, F_s):
        raise NotImplementedError


class ActorCritic(BasePolicy):
    def __repr__(self):
        return "Actor"

    def __init__(self, ground_policy, F_s, F_sa, env, device, log, hyperparameters):
        super(ActorCritic, self).__init__(ground_policy, F_s, F_sa, env, device, log, hyperparameters)
        self.F_s = F_s

    def optimize_networks(self, transitions):
        # TODO: possible have an action normalizer? For state_features we could have a batchnorm layer, maybe it is better for both
        # TODO: for HRL this might be nice
        state_features = transitions["state_features"]
        action_batch = transitions["action"]

        state_action_features = self.F_sa(state_features, action_batch)
        transitions["state_action_features"] = state_action_features
        if self.Q is not None:
            self.Q.retain_graph = True
        if self.V is not None:
            self.V.retain_graph = True
        TDE = self.train_critic(self.Q, self.V, transitions)

        error = self.train_actor(transitions)

        return TDE + error

    def init_actor(self, Q, V, F_s):
        actor = Actor(F_s, self.env, self.log, self.device, self.hyperparameters)
        actor.Q = Q
        actor.V = V
        # Q.target_net.actor = actor

        # print(actor)
        return actor

    def train_actor(self, transitions):
        error = self.actor.optimize(transitions)
        return abs(error)


class Q_Policy(BasePolicy):
    def __init__(self, ground_policy, F_s, F_sa, env, device, log, hyperparameters):
        super(Q_Policy, self).__init__(ground_policy, F_s, F_sa, env, device, log, hyperparameters)
        self.critic = self.Q

    def optimize_networks(self, transitions, retain_graph=False):
        TDE = self.train_critic(self.Q, self.V, transitions)
        return TDE

    def init_actor(self, Q, V, F_s):
        return Q


# 1. For ensemble: simply create many base policies, train them all with .optimize() and sum action output.
#    When creating policies, have them share seam feature processor (possibly create one in this class and pass it to base policies)
# 2. For REM: pick random subset to train
# 3. For new idea with Matthias: have additional SOM
class REM(BasePolicy):
    def __init__(self, ground_policy, F_s, F_sa, env, device, log, hyperparameters):
        super(REM, self).__init__(ground_policy, F_s, F_sa, env, device, log, hyperparameters)

        self.num_heads = hyperparameters["REM_num_heads"]
        self.num_samples = hyperparameters["REM_num_samples"]

        # Create ensemble of ground policies:
        # TODO: maybe let all the critics of the ground policy share their reward nets if they are split
        self.policy_heads = [ground_policy(None, F_s, F_sa, env, device, log, hyperparameters)
                             for _ in range(self.num_heads)]
        for head in self.policy_heads:
            head.set_retain_graph(True)

    def init_actor(self, Q, V, F_s):
        return None

    def init_critic(self, F_s, F_sa):
        return None, None

    # TODO: atm we iterate through the list of sampled base_policies - can this be done in a better way? MPI, GPU like?
    def optimize_networks(self, transitions):
        idxes = random.sample(range(self.num_heads), self.num_samples)
        error = 0
        for idx in idxes:
            current_policy = self.policy_heads[idx]
            # TODO: here we might bootstrap over our batch to have slightly different training data per policy!
            error += current_policy.optimize_networks(transitions)
        return error / self.num_samples

    def choose_action(self, state, calc_state_features=True):
        # Preprocess:
        if calc_state_features:
            state_features = self.F_s(state)
        else:
            state_features = state
        # Select random subset to output action:
        idxes = random.sample(range(self.num_heads), self.num_samples)
        summed_action = None
        for idx in idxes:
            current_policy = self.policy_heads[idx]
            with torch.no_grad():
                action = current_policy.actor(state_features)
            if summed_action is None:
                summed_action = action
            else:
                summed_action += action
        return summed_action / self.num_samples

    def update_targets(self, n_steps):
        # TODO: test whether sampling here could also be beneficially (might need to drop target network update steps for it)
        idxes = range(self.num_heads)
        # idxes = random.sample(range(self.num_heads), self.num_samples)
        for idx in idxes:
            self.policy_heads[idx].update_targets(n_steps)

    def calculate_TDE(self, state, action, next_state, reward, done):
        # TOOD: implement
        return torch.tensor([0])

    def display_debug_info(self):
        pass


class MineRLPolicy(BasePolicy):
    def __init__(self, ground_policy, base_policy, F_s, F_sa, env, device, log, hyperparameters):
        super(MineRLPolicy, self).__init__(ground_policy, F_s, F_sa, env, device, log, hyperparameters)

        self.base_policy = base_policy

        self.jump_options = env.jump_options
        self.attack_options = env.attack_options
        self.lateral_options = env.lateral_options
        self.straight_options = env.straight_options
        self.camera_x_options = env.camera_x_options_string
        self.camera_y_options = env.camera_y_options_string

        self.num_jump_actions = len(self.jump_options)
        self.num_attack_actions = len(self.attack_options)
        self.num_lateral_actions = len(self.lateral_options)
        self.num_straight_actions = len(self.straight_options)
        self.num_camera_x_actions = len(self.camera_x_options)
        self.num_camera_y_actions = len(self.camera_y_options)
        self.num_camera_actions = self.num_camera_x_actions * self.num_camera_y_actions
        self.num_move_actions = self.num_jump_actions * self.num_attack_actions * self.num_lateral_actions * \
                                self.num_straight_actions * self.num_camera_actions

    def create_adjusted_action_policy(self, num_actions, shift, action_mapping, counter, move_policy=False):
        action_space = Discrete(num_actions)
        real_action_space = self.env.action_space
        self.env.action_space = action_space
        if move_policy:
            new_policy = MineRLMovePolicy(self.ground_policy, self.base_policy, self.F_s, self.F_sa, self.env,
                                          self.device,
                                          self.log, self.hyperparameters)
        else:
            new_policy = self.base_policy(self.ground_policy, self.F_s, self.F_sa, self.env, self.device, self.log,
                                          self.hyperparameters)
        new_policy.set_retain_graph(True)
        new_policy.shift = shift
        action_mapping.extend([counter for _ in range(num_actions)])
        self.env.action_space = real_action_space
        return new_policy, shift + num_actions

    def init_actor(self, Q, V, F_s):
        return None

    def init_critic(self, F_s, F_sa):
        return None, None

    def calculate_TDE(self, state, action, next_state, reward, done):
        # TODO: implement
        return torch.tensor([0])

    def display_debug_info(self):
        pass


class MineRLObtainPolicy(MineRLPolicy):
    def __init__(self, ground_policy, base_policy, F_s, F_sa, env, device, log, hyperparameters):
        super(MineRLObtainPolicy, self).__init__(ground_policy, base_policy, F_s, F_sa, env, device, log,
                                                 hyperparameters)

        # Create policies:
        print("Creating high-level policy:")
        self.decider, _ = self.create_adjusted_action_policy(6, 0, [], 0)
        shift = 0
        self.action_mapping = []
        print("Creating low-level policies:")
        self.mover, shift = self.create_adjusted_action_policy(9, shift, self.action_mapping, 0)
        self.placer, shift = self.create_adjusted_action_policy(6, shift, self.action_mapping, 1)
        self.equipper, shift = self.create_adjusted_action_policy(7, shift, self.action_mapping, 2)
        self.crafter, shift = self.create_adjusted_action_policy(4, shift, self.action_mapping, 3)
        self.nearby_crafter, shift = self.create_adjusted_action_policy(7, shift, self.action_mapping, 4)
        self.nearby_smelter, shift = self.create_adjusted_action_policy(2, shift, self.action_mapping, 5)
        print()

        self.lower_level_policies = (self.mover, self.placer, self.equipper, self.crafter, self.nearby_crafter,
                                     self.nearby_smelter)

    def action2high_low_level(self, actions):
        high_lvl = []
        low_lvl = []
        for action in actions:
            action = action.item()
            high_lvl_action = self.action_mapping[action]
            low_lvl_action = action - self.lower_level_policies[high_lvl_action].shift
            high_lvl.append(high_lvl_action)
            low_lvl.append(low_lvl_action)
        high_lvl = torch.tensor(high_lvl).unsqueeze(1)
        low_lvl = torch.tensor(low_lvl).unsqueeze(1)
        return high_lvl, low_lvl

    def get_masks(self, actions, num_low_lvl=6):
        # Aggregate idxs for lower-level policies to operate on:
        idxs = [[] for _ in range(num_low_lvl)]
        for idx, action in enumerate(actions):
            idxs[action.item()].append(idx)
        return idxs

    def optimize_networks(self, transitions):
        error = 0
        # Save actions:
        original_actions = transitions["action_argmax"].clone()
        # Transform action idx such as 34 into e.g. ([3], [8])
        high_level_actions, low_level_actions = self.action2high_low_level(original_actions)
        # Train high-level policy:
        transitions["action_argmax"] = high_level_actions
        error += self.decider.optimize_networks(transitions)
        # Get mask of which low-level policy trains on which part of the transitions:
        mask_list = self.get_masks(high_level_actions)
        # Train low-level policies:
        for policy_idx, idx_mask in enumerate(mask_list):
            if not idx_mask:
                continue
            transitions["action_argmax"][idx_mask] = low_level_actions[idx_mask]
            # Apply mask to transition dict and dicts within dict:
            partial_transitions = {
                key: None if transitions[key] is None else transitions[key] if isinstance(transitions[key], list) else {
                    sub_key: transitions[key][sub_key][idx_mask] for sub_key in transitions[key]} if isinstance(
                    transitions[key], dict) else transitions[key][idx_mask] for key in transitions}
            policy = self.lower_level_policies[policy_idx]
            error[idx_mask] += policy.optimize_networks(partial_transitions)
        # Reset actions just in case:
        transitions["action_argmax"] = original_actions
        return error

    def apply_lower_level_policy(self, policy, state):
        with torch.no_grad():
            actions_vals = policy(state)
        action_idxs = torch.argmax(actions_vals, dim=1) + policy.shift
        return action_idxs

    def choose_action(self, state, calc_state_features=True):
        # Preprocess:
        if calc_state_features:
            state_features = self.F_s(state)
        else:
            state_features = state
        # Preprocess:
        action_q_vals = torch.zeros(state_features.shape[0], self.num_actions)
        # Apply high-level policy:
        with torch.no_grad():
            action = self.decider.choose_action(state_features, calc_state_features=False)
        high_level_actions = torch.argmax(action, dim=1)
        # print("High level actions: ", high_level_actions)
        masks = self.get_masks(high_level_actions)
        # print("Masks: ", masks)
        # Apply lower-level policies:
        for policy_idx, mask in enumerate(masks):
            if not mask:
                continue
            # print("Mask: ", mask)
            # print("state shape: ", state_features.shape)
            # print("action q vals masked shape: ", action_q_vals[mask].shape)
            # print("action q vals masked: ", action_q_vals[mask])
            # print("state masked shape: ", state_features[mask].shape)
            policy = self.lower_level_policies[policy_idx]
            shift = policy.shift
            low_lvl_action = policy.choose_action(state_features, calc_state_features=False)
            # print("low level action shape: ", low_lvl_action.shape)
            # print("low level action: ", low_lvl_action)
            action_q_vals[0][shift: shift + len(low_lvl_action[0])] = low_lvl_action[0]
        return action_q_vals

    def update_targets(self, n_steps):
        self.decider.update_targets(n_steps)
        for policy in self.lower_level_policies:
            policy.update_targets(n_steps)


class MineRLMovePolicy(MineRLPolicy):
    def __init__(self, ground_policy, base_policy, F_s, F_sa, env, device, log, hyperparameters):
        super(MineRLMovePolicy, self).__init__(ground_policy, base_policy, F_s, F_sa, env, device, log, hyperparameters)

        self._noop_template = env.noop
        print("Creating Move Policy: ")
        self.attacker = self.create_adjusted_action_policy(self.attack_options)
        self.lateralus = self.create_adjusted_action_policy(self.lateral_options)
        self.straightener = self.create_adjusted_action_policy(self.straight_options)
        self.jumper = self.create_adjusted_action_policy(self.jump_options)
        self.camera_xer = self.create_adjusted_action_policy(self.camera_x_options)
        self.camera_yer = self.create_adjusted_action_policy(self.camera_y_options)
        self.policies = [self.attacker, self.lateralus, self.straightener, self.jumper, self.camera_xer,
                         self.camera_yer]
        print()

    def create_adjusted_action_policy(self, options):
        action_space = Discrete(len(options))
        real_action_space = self.env.action_space
        self.env.action_space = action_space
        new_policy = self.base_policy(self.ground_policy, self.F_s, self.F_sa, self.env, self.device, self.log,
                                      self.hyperparameters)
        new_policy.set_retain_graph(True)
        for idx, option in enumerate(options):
            if "none" in option:
                options[idx] = None

        new_policy.options = options
        self.env.action_space = real_action_space
        return new_policy

    def get_policy_options(self, policy, state):
        action = policy.choose_action(state, calc_state_features=False)
        action_idxs = torch.argmax(action, dim=1)
        return [policy.options[idx] for idx in action_idxs]

    def apply_options(self, options, noops):
        actions = []
        for idx, noop in enumerate(noops):
            options_to_apply = [option[idx] for option in options]
            for option in options_to_apply:
                if option is None:
                    continue
                elif option[0] == "x":
                    noop["camera"][0] = int(option[2:])
                elif option[0] == "y":
                    noop["camera"][1] = int(option[2:])
                else:
                    noop[option] = 1
            actions.append(noop)
        return actions

    def choose_action(self, state, calc_state_features=True):
        # Preprocess:
        if calc_state_features:
            state_features = self.F_s(state)
        else:
            state_features = state
        # Init q val tensor and action templates
        action_q_vals = torch.zeros(self.num_actions)
        noops = [copy.deepcopy(self._noop_template) for _ in range(state_features.shape[0])]
        # Apply policies and extract semantics:
        options = [self.get_policy_options(policy, state_features) for policy in self.policies]
        actions = self.apply_options(options, noops)
        # Transform actions to match output format:
        action_idx = self.env.dicts2idxs(actions)
        # print("in choose_action of MovePOlicy")
        # print("action q vals shape: ", action_q_vals.shape)
        # print("actoin idx: ", action_idx)
        action_q_vals[
            action_idx] = 1  # Hacky way so that this action is chosen, as the interface requires us to return Q-vals for all possible actions
        # print()
        return action_q_vals.unsqueeze(0)

    def get_action_idxs_for_policy(self, policy, action_dicts):
        action_idxs = torch.zeros(len(action_dicts), device=self.device).long()
        for dict_idx, action_dict in enumerate(action_dicts):
            action_idx = None
            none_idx = None
            for idx, option in enumerate(policy.options):
                if option is None:
                    none_idx = idx
                elif option[0] == "x":
                    number = float(option[2:])
                    if action_dict["camera"][0] == number:
                        action_idx = idx
                        break
                elif option[0] == "y":
                    number = float(option[2:])
                    if action_dict["camera"][1] == number:
                        action_idx = idx
                        break
                else:
                    if action_dict[option] == 1:
                        action_idx = idx
                        break
            if action_idx is None:
                action_idx = none_idx
            action_idxs[dict_idx] = action_idx
        return action_idxs.unsqueeze(1)

    def optimize_networks(self, transitions):
        error = 0
        # Save actions:
        original_actions = transitions["action_argmax"].clone()
        # Transform actions in dicts:
        action_dicts = [self.env.action(idx.item()) for idx in original_actions]
        for policy in self.policies:
            action_idxs = self.get_action_idxs_for_policy(policy, action_dicts)
            transitions["action_argmax"] = action_idxs
            error += policy.optimize_networks(transitions)
        transitions["action_argmax"] = original_actions
        return error

    def update_targets(self, n_steps):
        for policy in self.policies:
            policy.update_targets(n_steps)


class MineRLHierarchicalPolicy(MineRLPolicy):
    def __init__(self, ground_policy, base_policy, F_s, F_sa, env, device, log, hyperparameters):
        super(MineRLHierarchicalPolicy, self).__init__(ground_policy, base_policy, F_s, F_sa, env, device, log,
                                                       hyperparameters)

        # Create policies:
        noop = self.env.noop

        shift = 0
        count = 0
        self.action_mapping = []
        print("Creating low-level policies:")
        self.mover, shift = self.create_adjusted_action_policy(self.num_move_actions, shift, self.action_mapping, count,
                                                               move_policy=True)
        count += 1
        self.lower_level_policies = [self.mover]
        if "place" in noop:
            num_choices = self.env.wrapping_action_space.spaces["place"].n - 1
            self.placer, shift = self.create_adjusted_action_policy(num_choices, shift, self.action_mapping, count)
            self.lower_level_policies.append(self.placer)
            count += 1
        if "equip" in noop:
            num_choices = self.env.wrapping_action_space.spaces["equip"].n - 1
            self.equipper, shift = self.create_adjusted_action_policy(num_choices, shift, self.action_mapping, count)
            self.lower_level_policies.append(self.equipper)
            count += 1
        if "craft" in noop:
            num_choices = self.env.wrapping_action_space.spaces["craft"].n - 1
            self.crafter, shift = self.create_adjusted_action_policy(num_choices, shift, self.action_mapping, count)
            self.lower_level_policies.append(self.crafter)
            count += 1
        if "nearbyCraft" in noop:
            num_choices = self.env.wrapping_action_space.spaces["nearbyCraft"].n - 1
            self.nearby_crafter, shift = self.create_adjusted_action_policy(num_choices, shift, self.action_mapping,
                                                                            count)
            self.lower_level_policies.append(self.nearby_crafter)
            count += 1
        if "nearbySmelt" in noop:
            num_choices = self.env.wrapping_action_space.spaces["nearbySmelt"].n - 1
            self.nearby_smelter, shift = self.create_adjusted_action_policy(num_choices, shift, self.action_mapping,
                                                                            count)
            self.lower_level_policies.append(self.nearby_smelter)
            count += 1
        if len(self.lower_level_policies) > 1:
            print("Creating high-level policy:")
            self.decider, _ = self.create_adjusted_action_policy(len(self.lower_level_policies), 0, [], 0)
        else:
            self.decider = None
        print()

    def action2high_low_level(self, actions):
        high_lvl = []
        low_lvl = []
        for action in actions:
            action = action.item()
            high_lvl_action = self.action_mapping[action]
            low_lvl_action = action - self.lower_level_policies[high_lvl_action].shift
            # if high_lvl_action != 0:
            # print("raw action: ", action)
            # print("high level action: ", high_lvl_action)
            # print("low level action: ", low_lvl_action)
            high_lvl.append(high_lvl_action)
            low_lvl.append(low_lvl_action)
        high_lvl = torch.tensor(high_lvl).unsqueeze(1)
        low_lvl = torch.tensor(low_lvl).unsqueeze(1)
        return high_lvl, low_lvl

    def get_masks(self, actions, num_low_lvl=6):
        # Aggregate idxs for lower-level policies to operate on:
        idxs = [[] for _ in range(num_low_lvl)]
        for idx, action in enumerate(actions):
            idxs[action.item()].append(idx)
        return idxs

    def apply_mask_to_transitions(self, transitions, idx_mask):
        masked_transitions = {}

        # Deal with non final next states:
        if transitions["non_final_mask"] is None:
            masked_transitions["non_final_mask"] = None
        else:
            non_final_mask = transitions["non_final_mask"]

            def apply_idx_mask_to_mask(idx_mask, mask):
                non_final_idx = 0
                transformed_mask = []  # torch.zeros(non_finals.shape ,dtype=torch.bool)
                idx_mask_cpy = idx_mask[:]
                # Iterate through the non_final_mask to find out which non_final_next states are masked by the idx_mask:
                for mask_idx, non_final_bool in enumerate(mask):
                    if non_final_bool:
                        if mask_idx == idx_mask_cpy[0]:
                            transformed_mask.append(non_final_idx)
                        non_final_idx += 1
                    if mask_idx == idx_mask_cpy[0]:
                        del idx_mask_cpy[0]  # as we iterate from the start the first idx in idx mask is always deleted
                        if len(idx_mask_cpy) == 0:
                            break
                return transformed_mask

            transformed_mask = apply_idx_mask_to_mask(idx_mask, non_final_mask)

            non_finals = transitions["non_final_next_states"]
            masked_transitions["non_final_next_states"] = {key: non_finals[key][transformed_mask] for key in non_finals}
            masked_transitions["non_final_next_state_features"] = transitions["non_final_next_state_features"][transformed_mask]
            masked_transitions["non_final_mask"] = transitions["non_final_mask"][idx_mask]

        # Deal with the rest:
        for key in transitions:
            content = transitions[key]
            if content is None:
                new_content = None
            elif key in ("non_final_mask", "non_final_next_states", "non_final_next_state_features"):
                continue
            elif isinstance(content, list):
                new_content = content  # For PER idx
            elif isinstance(content, dict):
                new_dict = {}
                for sub_key in content:
                    sub_content = content[sub_key]
                    new_sub_content = sub_content[idx_mask]
                    new_dict[sub_key] = new_sub_content
                new_content = new_dict
            else:
                new_content = content[idx_mask]
            masked_transitions[key] = new_content

        return masked_transitions

    def optimize_networks(self, transitions):
        error = 0
        # Save actions:
        original_actions = transitions["action_argmax"].clone()
        # Transform action idx such as 34 into e.g. ([3], [8])
        high_level_actions, low_level_actions = self.action2high_low_level(original_actions)
        # Train high-level policy:
        if self.decider is not None:
            transitions["action_argmax"] = high_level_actions
            error += self.decider.optimize_networks(transitions)
        else:
            error = torch.zeros(len(transitions, device=self.device))
        # Get mask of which low-level policy trains on which part of the transitions:
        mask_list = self.get_masks(high_level_actions)
        # Train low-level policies:
        for policy_idx, idx_mask in enumerate(mask_list):
            if not idx_mask:
                continue
            transitions["action_argmax"][idx_mask] = low_level_actions[idx_mask]
            # Apply mask to transition dict and dicts within dict:
            partial_transitions = self.apply_mask_to_transitions(transitions, idx_mask)
            policy = self.lower_level_policies[policy_idx]
            error[idx_mask] += policy.optimize_networks(partial_transitions)
        # Reset actions just in case:
        transitions["action_argmax"] = original_actions
        return error

    def apply_lower_level_policy(self, policy, state):
        with torch.no_grad():
            actions_vals = policy(state)
        action_idxs = torch.argmax(actions_vals, dim=1) + policy.shift
        return action_idxs

    def choose_action(self, state):
        # Preprocess:
        state_features = self.F_s(state)
        # Preprocess:
        action_q_vals = torch.zeros(state_features.shape[0], self.num_actions)
        # Apply high-level policy:
        with torch.no_grad():
            action = self.decider.choose_action(state_features, calc_state_features=False)
        high_level_actions = torch.argmax(action, dim=1)
        # print("High level actions: ", high_level_actions)
        masks = self.get_masks(high_level_actions)
        # print("Masks: ", masks)
        # Apply lower-level policies:
        for policy_idx, mask in enumerate(masks):
            if mask == []:
                continue
            # print("Mask: ", mask)
            # print("state shape: ", state_features.shape)
            # print("action q vals masked shape: ", action_q_vals[mask].shape)
            # print("action q vals masked: ", action_q_vals[mask])
            # print("state masked shape: ", state_features[mask].shape)
            policy = self.lower_level_policies[policy_idx]
            shift = policy.shift
            low_lvl_action = policy.choose_action(state_features, calc_state_features=False)
            # print("low level action shape: ", low_lvl_action.shape)
            # print("low level action: ", low_lvl_action)
            # print("low_lvl_ action: ", low_lvl_action)
            # print("shift of policy: ", shift)
            # print("low lvl action len : ", (len(low_lvl_action[0])))
            # print("policy idx: ", policy_idx)
            action_q_vals[0][shift: shift + len(low_lvl_action[0])] = low_lvl_action[0]
        return action_q_vals

    def update_targets(self, n_steps):
        self.decider.update_targets(n_steps)
        for policy in self.lower_level_policies:
            policy.update_targets(n_steps)

    def calculate_TDE(self, state, action, next_state, reward, done):
        # TODO: implement
        return torch.tensor([0])

    def display_debug_info(self):
        pass
