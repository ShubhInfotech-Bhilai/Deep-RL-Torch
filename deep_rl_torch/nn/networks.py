import torch
import torch.nn.functional as F

from .nn_utils import calc_gradient_norm, calc_norm, soft_update, hard_update


class OptimizableNet(torch.nn.Module):
    # def __repr__(self):
    # TODO: return summary using pytorch
    #    return str(self.type)

    def __init__(self, env, device, log, hyperparameters, is_target_net=False):
        super(OptimizableNet, self).__init__()
        self.env = env
        self.log = log
        self.device = device
        self.hyperparameters = hyperparameters
        self.verbose = hyperparameters["verbose"]

        # Env Action Space:
        self.discrete_env = True if "Discrete" in str(env.action_space) else False
        if self.discrete_env:
            self.num_actions = env.action_space.n
            self.action_low = torch.zeros(self.num_actions)
            self.action_high = torch.ones(self.num_actions)
        else:
            self.num_actions = len(env.action_space.low)
            self.action_low = torch.tensor(env.action_space.low)
            self.action_high = torch.tensor(env.action_space.high)

        # To scale the gradient in optimization:
        self.head_count = 0

        # Load hyperparameters:
        if is_target_net:
            self.use_target_net = False
        else:
            self.use_target_net = hyperparameters["use_target_net"]
        self.retain_graph = False
        self.optimize_centrally = hyperparameters["optimize_centrally"]
        self.max_norm = hyperparameters["max_norm"]
        self.batch_size = hyperparameters["batch_size"]
        self.optimizer = hyperparameters["optimizer"]
        # Actor:
        self.use_actor_critic = hyperparameters["use_actor_critic"]
        self.use_CACLA_V = hyperparameters["use_CACLA_V"]
        self.use_CACLA_Q = hyperparameters["use_CACLA_Q"]
        self.use_DDPG = hyperparameters["use_DDPG"]
        self.use_SPG = hyperparameters["use_SPG"]
        self.use_GISPG = hyperparameters["use_GISPG"]

        # Target net:
        self.target_network_polyak = hyperparameters["use_polyak_averaging"]
        if self.target_network_polyak:
            self.tau = hyperparameters["polyak_averaging_tau"]
        self.target_network_hard_steps = hyperparameters["target_network_hard_steps"]

    def compute_loss(self, output, target, sample_weights):
        loss = F.smooth_l1_loss(output, target, reduction='none')
        if sample_weights is None:
            reduced_loss = loss.mean()
        else:
            reduced_loss = (loss * sample_weights).mean()
        return loss, reduced_loss

    def optimize_net(self, output, target, optimizer, name="", sample_weights=None, retain_graph=False):
        """Start loss calculation and optimize parameters if they are not optimized centrally"""
        loss, reduced_loss = self.compute_loss(output, target, sample_weights)

        if not self.optimize_centrally:
            optimizer.zero_grad()
            reduced_loss.backward(retain_graph=self.retain_graph + retain_graph)
            self.scale_gradient()
            self.norm_gradient()
            optimizer.step()

            self.log_nn_data()

        name = "losses/loss_" + self.name + (("_" + name) if name != "" else "")
        detached_loss = reduced_loss.detach().clone().item()
        self.log.add(name, detached_loss, use_skip=True)

        PER_weights = loss.detach().clone().cpu()

        # Increment counter in the feature extractors to scale their gradients later on:
        if self.F_s is not None:
            self.F_s.increment_head_counter()
        if self.F_sa is not None:
            self.F_sa.increment_head_counter()

        return PER_weights, reduced_loss

    def get_updateable_params(self):
        return self.parameters()

    def update_targets(self, steps):
        """Update weights of the target networks."""
        if self.target_network_polyak:
            soft_update(self, self.target_net, self.tau)
        else:
            if steps % self.target_network_hard_steps == 0:
                hard_update(self, self.target_net)

    def create_target_net(self):
        """Create a target network of itself with frozen weights"""
        target_net = None
        if self.use_target_net:
            target_net = self.recreate_self()
            for param in target_net.parameters():
                param.requires_grad = False
            target_net.use_target_net = False
            target_net.eval()
        return target_net

    def log_layer_data(self, layers, name, extra_name=""):
        """Log diagnostic information on the NN."""
        if self.log.is_available("NN_diagnostics", factor=10):
            name = name + " " + extra_name if extra_name else name
            weight_norm = calc_norm(layers)
            grad_norm = calc_gradient_norm(layers)
            self.log.add("Weight Norm/" + name, weight_norm)
            self.log.add("Grad Norm/" + name, grad_norm)
            name += "_" + extra_name + "_" if extra_name else ""

            if self.log.is_available("NN_distributions", skip_steps=5):
                weights = torch.cat([torch.flatten(layer).detach() for layer in layers.parameters()])\
                    .view(-1)
                gradients = torch.cat([torch.flatten(layer.grad.data).detach() for layer in layers.parameters()])\
                    .view(-1)
                self.log.add("Weights/" + name, weights, distribution=True)
                self.log.add("Gradients/" + name, gradients, distribution=True)

    def norm_gradient(self):
        if self.max_norm:
            torch.nn.utils.clip_grad.clip_grad_norm_(self.parameters(), self.max_norm)

    def scale_gradient(self):
        """Scales the gradient of this network based on how many networks let their gradients flow into it"""
        params = self.get_updateable_params()
        for layer in params:
            layer.grad.data = layer.grad.data / self.head_count
        # Reset head count:
        self.head_count = 0

    def increment_head_counter(self):
        self.head_count += 1


