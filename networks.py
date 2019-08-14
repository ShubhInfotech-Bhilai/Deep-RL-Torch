import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import itertools
from util import *

import math
import copy
import numpy as np


def conv2d_size_out(size, kernel_size=5, stride=2):
    return (size - (kernel_size - 1) - 1) // stride + 1


# TODO: possibly put activation functions per layer into some other list...
def create_ff_layers(first_layer_input, layer_dict):
    input_size = first_layer_input
    layers = nn.ModuleList()
    for layer in layer_dict:
        this_layer_neurons = layer["neurons"]
        if layer["name"] == "linear":
            layers.append(nn.Linear(input_size, this_layer_neurons))
        elif layer["name"] == "LSTM":
            layers.append(nn.LSTM(input_size, this_layer_neurons))
        elif layer["name"] == "GRU":
            layers.append(nn.GRU(input_size, this_layer_neurons))
        input_size = this_layer_neurons
    return layers, input_size


# Create a module list of conv layers specified in layer_dict
def create_conv_layers(input_matrix_shape, layer_dict):
    # format for entry in matrix_layers: ("conv", channels_in, channels_out, kernel_size, stride) if conv or
    #  ("batchnorm") for batchnorm
    matrix_width = input_matrix_shape[0]
    matrix_height = input_matrix_shape[1]
    channel_last_layer = input_matrix_shape[2]

    layers = nn.ModuleList()
    for layer in layer_dict:
        if layer["name"] == "batchnorm":
            layers.append(nn.BatchNorm2d(channel_last_layer))
        elif layer["name"] == "conv":
            this_layer_channels = layer["channels_out"]
            layers.append(nn.Conv2d(channel_last_layer, this_layer_channels, layer["kernel_size"],
                                    layer["stride"]))
            matrix_width = conv2d_size_out(matrix_width, layer["kernel_size"], layer["stride"])
            matrix_height = conv2d_size_out(matrix_height, layer["kernel_size"], layer["stride"])
            channel_last_layer = this_layer_channels

    conv_output_size = matrix_width * matrix_height * channel_last_layer

    return layers, conv_output_size


class OptimizableNet(nn.Module):
    def __repr__(self):
        # TODO: return summary using pytorch
        return self.type

    def __init__(self, log, optimizer):
        self.log = log
        self.optimizer = optimizer
        # TODO: initialize customizable loss function here (especially for actor!)

    def optimize_net(self, output, target, optimizer, name=""):
        loss = F.smooth_l1_loss(output, target.unsqueeze(1))

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        name = "loss_" + self.name + (("_" + name) if name != "" else "")
        self.log.add(name, loss.detach())

        # Compute Temporal-Difference Errors:
        temporal_diff_errors = target - output

        return temporal_diff_errors.detach()


class ProcessState(OptimizableNet):
    def __init__(self, vector_len, matrix_shape, vector_layers, matrix_layers,
                 merge_layers, activation_function, matrix_max_val=255):
        super(ProcessState, self).__init__()
        self.act_func = activation_function
        self.vector_normalizer = Normalizer(vector_len)
        self.matrix_normalizer = Normalizer(matrix_shape, matrix_max_val)

        vector_output_size = 0
        matrix_output_size = 0

        if vector_len is not None:
            self.vector_layers, vector_output_size = create_ff_layers(vector_len, vector_layers)

        # matrix size has format (x_len, y_len, n_channels)
        if matrix_shape is not None:
            self.matrix_layers, matrix_output_size = create_conv_layers(matrix_shape, matrix_layers)

        # format for parameters: ["linear": (input, output neurons), "lstm": (input, output neurons)]
        merge_input_size = vector_output_size + matrix_output_size
        self.merge_layers = create_ff_layers(merge_input_size, merge_layers)

    def forward(self, vector, matrix):
        # TODO: instead of having two inputs, only have one state to make the function more general.
        # TODO: to separate the state into matrix and vector, check the number of dimensions of the state
        merged = torch.tensor([])
        if matrix is not None:
            batch_size = matrix.size(0)
            for layer in self.matrix_layers:
                matrix = self.act_func(layer(matrix))
            matrix = matrix.view(batch_size, -1)
            merged = torch.cat((merged, matrix), 0)

        if vector is not None:
            for layer in self.vector_layers:
                vector = self.act_func(layer(vector))
            merged = torch.cat((merged, vector), 0)

        for layer in self.merge_layers:
            merged = self.act_func(layer(merged))
        return merged


class ProcessStateAction(OptimizableNet):
    def __init__(self, state_features_len, action_len, layers, activation_function):
        super(ProcessStateAction, self).__init__()

        self.act_func = activation_function

        # TODO: possible have an action normalizer? For state_features we could have a batchnorm layer, maybe it is better for both

        input_size = state_features_len + action_len

        self.layers, output_vector_size = create_ff_layers(input_size, layers)

    def forward(self, state_features, actions):
        x = torch.cat((state_features, actions), 0)
        for layer in self.layers:
            x = self.act_func(layer(x))
        return x


class TempDiffNet(OptimizableNet):
    def __init__(self, tau, use_polyak, target_network_hard_steps, split, split_layers, input_size, use_target_net, F_s):
        self.tau = tau
        self.target_network_polyak = use_polyak
        self.target_network_hard_steps = target_network_hard_steps
        self.split = split
        self.use_target_net = use_target_net

        self.current_reward_prediction = None

        

        self.reward_layers = self.create_reward_net()

        self.target_net = self.create_target_net()

        super(TempDiffNet, self).__init__(parameters, self.updateable_parameters)

    def create_reward_net(self):
        r_layers = None
        if self.split:
            r_layers, output_layer_input_neurons = create_ff_layers(input_size, split_layers)
            output_layer = nn.Linear(output_layer_input_neurons, self.output_neurons)
            r_layers.append(output_layer)
            # TODO: create optimizer for r network with different lr than Q net
        return r_layers

    def create_target_net(self):
        target_net = None
        if self.use_target_net:
            target_net = copy.deepcopy(self)
            # TODO: check if the following line makes sense - do we want different initial weights for the target network if we use polyak averaging?
            #target_net.apply(self.weights_init)
            target_net.use_target_net = False
            target_net.eval()
        return target_net

    def forward(self, x):
        predicted_reward = 0
        #TODO: what if reward and state_value should share a layer? ... I think the answer is that we share it via the F_s... but double check if that is enough!
        if self.r_layers:
            predicted_reward = x.copy()
            for layer in self.r_layers:
                predicted_reward = self.act_func(layer(y))
            self.last_r_prediction = predicted_reward

        for layer in self.layers:
            predicted_state_value = self.act_func(layer(x))

        return predicted_state_value + predicted_reward

    def forward_r(self, x):
        for layer in self.r_layers:
            x = self.act_func(layer(x))
        return x

    def forward_R(self, x):
        for layer in self.layers:
            x = self.act_func(layer(x))
        return x

    def weights_init(self, m):
        # if isinstance(m, nn.Conv2d):
        torch.nn.init.xavier_uniform(m.weight.data)

    # TODO: the following functions is just a model to modify the two functions below it appropriately
    def take_mean_weights_of_two_models(self, model1, model2):
        beta = 0.5  # The interpolation parameter
        params1 = model1.named_parameters()
        params2 = model2.named_parameters()

        dict_params2 = dict(params2)

        for name1, param1 in params1:
            if name1 in dict_params2:
                dict_params2[name1].data.copy_(beta * param1.data + (1 - beta) * dict_params2[name1].data)

        model.load_state_dict(dict_params2)

    def multiply_state_dict(self, state_dict, number):
        for i in state_dict:
            state_dict[i] *= number
        return state_dict

    def add_state_dicts(self, state_dict_1, state_dict_2):
        for i in state_dict_1:
            state_dict_1[i] += state_dict_2[i]
        return state_dict_1

    def update_target_network(self, steps):
        if self.target_network_polyak:
            # TODO: maybe find a better way than these awkward modify dict functions? (if they even work)
            self.target_net.load_state_dict(self.add_state_dicts(
                self.multiply_state_dict(self.target_net.state_dict(), (1 - self.tau)),
                self.multiply_state_dict(self.state_dict(), self.tau)))
        else:
            if steps % self.target_network_hard_steps == 0:
                self.target_net.load_state_dict(self.state_dict())

    def predict_current_state(self, state_features, state_action_features, actions):
        raise NotImplementedError

    def calculate_next_state_values(self, non_final_next_state_features, non_final_mask):
        next_state_values = torch.zeros(self.batch_size, device=self.device)
        next_state_predictions = self.target_net.predict_state_value(non_final_next_state_features,
                                                                     actor=self.actor).detach()
        next_state_values[non_final_mask] = next_state_predictions  # [0] #TODO: why [0]?
        return next_state_values

    def optimize(self, state_features, state_action_features, action_batch, reward_batch,
                        non_final_next_state_features, non_final_mask):
        # Compute V(s_t) or Q(s_t, a_t)
        predictions_current, reward_prediction = self.predict_current_state(state_features, state_action_features,
                                                                                 action_batch)
        # Train reward net if it exists:
        if self.split:
            self.optimize_net(reward_prediction, reward_batch, self.optimizer_reward, "r")
        # Compute V(s_t+1) or max_aQ(s_t+1, a) for all next states.
        predictions_next_state = self.predict_next_state(non_final_next_state_features, non_final_mask)

        # Compute the expected values. Do not add the reward, if the critic is split
        self.expected_value_next_state = (predictions_next_state * self.gamma) + (reward_batch if self.split else 0)

        self.TDE = self.optimize_net(predictions_current, self.expected_value_next_state, self.optimizer_TD)


class Q(TempDiffNet):
    def __init__(self, input_size, layers, num_actions, activation_function, F_s,
                 F_s_a, lr, hyperparameters):

        # can either have many outputs or one
        self.num_actions = num_actions
        self.multi_output = not hyperparameters["use_actor_critic"]
        self.output_neurons = num_actions if self.multi_output else 1
        # Network properties
        self.act_func = hyperparameters["activation_function"]
        self.lr = hyperparameters["lr_Q"]

        # Create layers
        self.layers, output_layer_input_neurons = create_ff_layers(input_size, layers)
        output_layer = nn.Linear(output_layer_input_neurons, self.output_neurons)
        self.layers.append(output_layer)

        # Define optimizer and previous networks
        # TODO: only optimize F_sa depedning on self.multi_output
        self.F_s = F_s
        self.F_s_a = F_s_a
        updateable_parameters = [self.parameters()] + (F_s.parameters() if F_s is not None else []) + \
                                (F_s_a.parameters() if F_s_a is not None else [])
        self.optimizer = optim.Adam(itertools.chain.from_iterable(updateable_parameters), lr=self.lr)

        super(Q, self).__init__()

    def predict_next_state(self, non_final_next_state_features, non_final_mask):
        if self.use_QVMAX:
            return self.V.calculate_next_state_values(non_final_next_state_features, non_final_mask)
        elif self.use_QV:
            # This assumes that V is always trained directly before Q
            return self.V_net.expected_value_next_state
        else:
            return self.calculate_next_state_values(non_final_next_state_features, non_final_mask)

    def predict_current_state(self, state_features, state_action_features, actions):
        reward_prediction = None
        if self.discrete:
            if self.split:
                reward_prediction = self.forward_r(state_features).gather(1, actions)
            return self.forward_R(state_features).gather(1, actions), reward_prediction  # .gather action that is taken
        else:
            if self.split:
                reward_prediction = self.forward_r(state_action_features)
            return self.forward_R(state_action_features), reward_prediction  # self.F_s_A(state_features, actions))

    def predict_state_value(self, state_features):
        if self.discrete:
            return self.forward(state_features).max(1)[0]
        else:
            with torch.no_grad():
                state_action_features = self.F_s_a(state_features, self.actor(state_features))
            # TODO: make sure whether these state-action_features are required somewhere else and store it if that is the case
            return self.predict_state_action_value(None, state_action_features, None)

    def predict_state_action_value(self, state_features, state_action_features, actions):
        if self.discrete:
            return self.forward(state_features).gather(1, actions)  # .gather action that is taken
        else:
            return self.forward(state_action_features)  # self.F_s_A(state_features, actions))


class V(TempDiffNet):
    def __init__(self, input_size, layers, activation_function, lr, F_s, hyperparameters):
        super(V, self).__init__()

        self.act_func = activation_function
        self.lr = lr
        self.output_neurons = 1

        self.use_QVMAX = hyperparameters["use_QVMAX"]

        # Create layers
        self.layers, output_layer_input_neurons = create_ff_layers(input_size, layers)
        output_layer = nn.Linear(output_layer_input_neurons, self.output_neurons)
        self.layers.append(output_layer)

        # Define optimizer and previous networks
        self.F_s = F_s
        updateable_parameters = [self.parameters()] + [(self.F_s.parameters() if self.F_s is not None else [])]
        self.optimizer = optim.Adam(itertools.chain.from_iterable(updateable_parameters), lr=self.lr)

    def predict_next_state(self, non_final_next_state_features, non_final_mask):
        if self.use_QVMAX:
            return self.Q.calculate_next_state_values(non_final_next_state_features, non_final_mask)
        else:
            return self.calculate_next_state_values(non_final_next_state_features, non_final_mask)

    def predict_state_value(self, state_features):
        with torch.no_grad():
            return self(state_features)

    def predict_current_state(self, state_features, state_action_features, actions):
        reward_prediction = None
        if self.split:
            reward_prediction = self.forward_r(state_features)
        return self.forward_R(state_features), reward_prediction


class Actor(OptimizableNet):
    def __init__(self, input_size, num_actions, discrete_actions, action_lows, action_highs, layers,
                 activation_function):
        super(Actor, self).__init__()
        self.activation_function = activation_function
        self.act_funcs_output_layer = []
        self.output_layers = nn.ModuleList()

        # Create layers
        self.layers, output_layer_input_neurons = create_ff_layers(input_size, layers)
        # Create output layers with different activation functions, depending on the range of action outputs:
        self.output_layers, self.act_funcs_output_layer = self.create_output_layers(discrete_actions, num_actions,
                                                                                    output_layer_input_neurons,
                                                                                    action_lows, action_highs)
        # Define optimizer and previous networks
        self.F_s = F_s
        updateable_parameters = [self.parameters()] + [(self.F_s.parameters() if self.F_s is not None else [])]
        self.optimizer = optim.Adam(itertools.chain.from_iterable(updateable_parameters), lr=self.lr)

    def forward(self, x):
        for layer in self.layers:
            x = self.activation_function(layer(x))

        outputs = []
        for i in range(len(self.output_layers)):
            outputs.append(self.act_funcs_output_layer[i](self.output_layers[i](x)))

        return torch.cat(outputs, dim=1)

    # TODO: the following function is too inefficient, as it creates a layer for every kind of activation function
    # TODO instead it should apply one weight matrix and apply different activation functions to the respective parts of it
    def create_output_layers(self, discrete_actions, num_actions, output_layer_input_neurons, action_lows,
                             action_highs):
        output_layers = []
        act_funcs_output_layer = []
        if discrete_actions:
            output_layer = nn.Linear(output_layer_input_neurons, num_actions)
            output_layers.append(output_layer)
            act_funcs_output_layer.append(F.sigmoid)
            output_layers.append(nn.Softmax(num_actions))
            act_funcs_output_layer.append(lambda x: x)
        else:
            # action bounds can be between (0, X), (-X, X), (-inf, inf)
            last_act_func = 0
            layer_size = 0
            for i in range(num_actions):
                low = action_lows[i]
                high = action_highs[i]
                # if one is zero
                if not (low and high):
                    multiplier = 1
                    if low == -math.inf or high == math.inf:
                        func = F.relu
                    else:
                        func = F.sigmoid
                        multiplier *= high + low
                    act_func = lambda x: func(x) * multiplier
                elif low == -1 * high:
                    if low == 1:
                        act_func = F.tanh
                    elif low == -math.inf:
                        act_func = lambda x: x
                    else:
                        act_func = lambda x: F.tanh(x) * high
                else:
                    act_func = lambda x: x.clamp(low, high)

                if act_func == last_act_func:
                    layer_size += 1
                else:
                    # finish last layer, create new one
                    if layer_size != 0:
                        output_layers.append(nn.Linear(output_layer_input_neurons, layer_size))
                        act_funcs_output_layer.append(last_act_func)

                    layer_size = 1
                    last_act_func = act_func
            if layer_size != 0:
                output_layers.append(nn.Linear(output_layer_input_neurons, layer_size))
                act_funcs_output_layer.append(last_act_func)

            return output_layers, act_funcs_output_layer

    def optimize(self, state_features, reward_batch, action_batch, non_final_next_states, non_final_mask, TDE_V,
                 Q_expectations_next_state):
        # Calculate current actions for state_batch:
        actions_current_state = self.actor(state_features)
        better_actions_current_state = actions_current_state.detach().copy()
        if self.USE_V_CACLA:
            # Requires TDE_V
            # Check which actions have a pos TDE
            pos_TDE_mask = self.V_net.TDE > 0
            better_actions_current_state[pos_TDE_mask] = action_batch[pos_TDE_mask]
        if self.USE_Q_CACLA:
            # Calculate mask of pos expected Q minus Q(s, mu(s))
            action_TDE = self.Q_net.expectations_next_state - self.Q_net(state_features, actions_current_state).detach()
            pos_TDE_mask = action_TDE > 0
            better_actions_current_state[pos_TDE_mask] = action_batch[pos_TDE_mask]
        # TODO: implement CACLA+Var

        # TODO - Idea: When using QV, possibly reinforce actions only if Q and V net agree (first check how often they disagree and in which cases)
        if self.USE_DDPG:
            # 1. calculate derivative of Q towards actions 2. Reinforce towards actions plus gradients

            q_vals = self.Q_net(state_features, actions_current_state)
            q_vals.backward()  # retain necessary?
            gradients = actions_current_state.grad
            # TODO: multiply gradients * -1 to ensure Q values increase? probably...
            # Normalize gradients:
            gradients = self.normalize_gradients(gradients)
            # TODO: maybe normalize within the actor optimizer...?
            # TODO Normalize over batch, then scale by inverse TDE (risky thing:what about very small TDEs?
            better_actions_current_state = actions_current_state + gradients

        if self.USE_SPG:
            # Calculate mask of Q(s,a) minus Q(s, mu(s))
            action_TDE = Q_pred_batch_state_action.detach()["Q"] - self.Q_net(state_batch, actions_current_state)
            pos_TDE_mask = action_TDE > 0
            better_actions_current_state[pos_TDE_mask] = action_batch[pos_TDE_mask]
            # 1. Get batch_actions and batch_best_actions (implement best_actions everywhere)
            # 2. Calculate eval of current action
            # 3. Compare batch_action and batch_best_actions to evals of current actions
            # 4. Sample around best action with Gaussian noise until better action is found, then sample around this
            # 5. Reinforce towards best actions
        if self.USE_GISPG:
            # Gradient Informed SPG
            # Option one:
            # Do SPG, but for every action apply DDPG to get the DDPG action and check if it is better than the non-
            # DDPG action.
            # Option two:
            # Do SPG, but do not sample with Gaussian noise. Instead always walk towards gradient of best action,
            #  with magnitude that decreases over one sampling period
            #
            #
            pass

        self.optimize_net(actions_current_state, better_actions_current_state, self.optimizer)
        # Train actor towards better actions (loss = better - current)