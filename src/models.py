"""
models.py

AUTHOR: Lucas Kabela

PURPOSE: This file defines Neural Network Architecture and other models
        which will be evaluated in this expirement
"""
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

# from os import path
from torch.distributions import Categorical, Normal
from utils import GumbelSoftmax, guard_q_actions


class SAC(nn.Module):
    """
    This network is a SAC network from
       > Soft Actor-Critic Algorithms and Applications, Haarnoja et al 2018

    Tricks include dual Q networks, adjusting entropy, and gumbel softmax for
    discrete
    """

    def __init__(self, env, tgt_ent=None, gamma=0.99, tau=1e-2, disc=False):
        super(SAC, self).__init__()
        if disc:
            self.actor = DiscreteActor(env)
        else:
            self.actor = Actor(env)
        self.discrete = disc
        self.soft_q1 = SoftQNetwork(env)
        self.soft_q2 = SoftQNetwork(env)
        self.tgt_q1 = SoftQNetwork(env).eval()
        self.tgt_q2 = SoftQNetwork(env).eval()

        if tgt_ent is None:
            self.target_entropy = -np.log(1.0 / env.action_space.n)
        else:
            self.target_entropy = tgt_ent
        self.log_alpha = torch.zeros(1, requires_grad=True)
        self.alpha = self.log_alpha.detach().exp()
        self.gamma = gamma
        self.tau = tau

    def get_action(self, state):
        return self.actor.get_action(state)

    def init_opt(self, opt="Adam", lr=3e-4):
        self.q1_opt = optim.Adam(self.soft_q1.parameters(), lr=lr)
        self.q2_opt = optim.Adam(self.soft_q2.parameters(), lr=lr)
        self.actor_opt = optim.Adam(self.actor.parameters(), lr=lr)
        self.entropy_opt = optim.Adam([self.log_alpha], lr=lr)

    def _freeze_tgt_networks(self):
        """
        Copy soft q networks into target q networks, and freeze parameters
        for training stability
        """
        q1 = zip(self.tgt_q1.parameters(), self.soft_q1.parameters())
        q2 = zip(self.tgt_q2.parameters(), self.soft_q2.parameters())

        # Copy parameters
        for target_param, param in q1:
            target_param.data.copy_(param.data)
        for target_param, param in q2:
            target_param.data.copy_(param.data)

        # Freeze gradients
        for param in self.tgt_q1.parameters():
            param.requires_grad = False
        for param in self.tgt_q2.parameters():
            param.requires_grad = False

    def soft_copy(self):
        q1_params = zip(self.tgt_q1.parameters(), self.soft_q1.parameters())
        q2_params = zip(self.tgt_q2.parameters(), self.soft_q2.parameters())
        for target_param, param in q1_params:
            target_param.data.copy_(
                target_param.data * (1.0 - self.tau) + param.data * self.tau
            )

        for target_param, param in q2_params:
            target_param.data.copy_(
                target_param.data * (1.0 - self.tau) + param.data * self.tau
            )

    def calc_critic_loss(self, states, actions, rewards, next_states, done):
        with torch.no_grad():
            advantage = self.actor.evaluate(next_states)
            next_probs, next_actions, _, _, _ = advantage
            next_actions = next_actions.unsqueeze(1)
            next_q1 = self.tgt_q1(next_states, next_actions)
            next_q2 = self.tgt_q2(next_states, next_actions)

            self.alpha = self.alpha.to(next_q1.device)
            min_q_next = torch.min(next_q1, next_q2) - self.alpha * next_probs
            target_q_value = rewards + (1 - done) * self.gamma * min_q_next

        p_q1 = self.soft_q1(states, actions)
        p_q2 = self.soft_q2(states, actions)
        q_value_loss1 = F.mse_loss(p_q1, target_q_value)
        q_value_loss2 = F.mse_loss(p_q2, target_q_value)
        return q_value_loss1, q_value_loss2

    def update_critics(self, q1_loss, q2_loss, clip=5.0):
        self.q1_opt.zero_grad()
        q1_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.soft_q1.parameters(), clip)
        self.q1_opt.step()

        self.q2_opt.zero_grad()
        q2_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.soft_q2.parameters(), clip)
        self.q2_opt.step()

    def calc_actor_loss(self, states):
        # Train actor network
        log_probs, actions, _, _, _ = self.actor.evaluate(states)
        q1 = self.soft_q1(states, actions)
        q2 = self.soft_q1(states, actions)
        min_q = torch.min(q1, q2)
        policy_loss = (self.alpha * log_probs - min_q).mean()
        return policy_loss, log_probs

    def update_actor(self, actor_loss, clip=5.0):
        self.actor_opt.zero_grad()
        actor_loss.backward()
        torch.nn.utils.clip_grad_norm_(SAC.actor.parameters(), clip)
        self.actor_opt.step()

    def calc_entropy_tuning_loss(self, log_probs):
        """
        Calculates the loss for the entropy temperature parameter.
        log_probs come from the return value of calculate_actor_loss
        """
        alpha_loss = -(
            self.log_alpha * (log_probs.detach() + self.target_entropy)
        ).mean()
        return alpha_loss

    def update_entropy(self, alpha_loss):
        self.entropy_opt.zero_grad()
        alpha_loss.backward()
        self.entropy_opt.step()
        self.alpha = self.log_alpha.detach().exp()

    def device(self):
        return next(self.parameters()).device


class SoftQNetwork(nn.Module):
    """
    Given an environment with |S| state dim and |A| actions, initialize
    a FFN with 2 hidden layers, and input size |S| + |A|.  Output a single
    Q value
    """

    def __init__(self, env, hidden=[128, 128], dropout=0.0):
        super(SoftQNetwork, self).__init__()
        self.state_space = env.observation_space.shape[0]
        self.action_space = env.action_space.n
        self.hidden = hidden

        self.l1 = nn.Linear(self.state_space + self.action_space, hidden[0])
        self.l2 = nn.Linear(hidden[0], hidden[1])
        self.l3 = nn.Linear(hidden[1], 1)

        self.ffn = nn.Sequential(
            self.l1,
            nn.Dropout(p=dropout),
            nn.ReLU(),
            self.l2,
            nn.Dropout(p=dropout),
            nn.ReLU(),
            self.l3,
        )
        self.init_weights()

    def init_weights(self):
        """
        Initialize weights with xaiver uniform, and
        fill bias with 1 over n
        """
        over_n = 1 / (self.state_space + self.action_space)
        nn.init.xavier_uniform_(self.l1.weight)
        self.l1.bias.data.fill_(over_n)
        nn.init.xavier_uniform_(self.l2.weight)
        self.l2.bias.data.fill_(1 / self.hidden[0])
        nn.init.xavier_uniform_(self.l3.weight)
        self.l3.bias.data.fill_(1 / self.hidden[1])

    def forward(self, state, action):
        """
        Given the state and action, produce a Q value
        """
        q_in = torch.cat([state, action], 1)
        return self.ffn(q_in).view(-1)

    def device(self):
        return next(self.parameters()).device


class Actor(nn.Module):
    def __init__(
        self,
        env,
        hidden=[128, 128],
        dropout=0.0,
        log_std_min=-20,
        log_std_max=2,
    ):
        super(Actor, self).__init__()
        self.state_space = env.observation_space.shape[0]
        self.action_space = env.action_space.n
        self.hidden = hidden
        self.log_std_min = log_std_min
        self.log_std_max = log_std_max

        self.l1 = nn.Linear(self.state_space, hidden[0])
        self.l2 = nn.Linear(hidden[0], hidden[1])
        self.ffn = nn.Sequential(
            self.l1,
            nn.Dropout(p=dropout),
            nn.ReLU(),
            self.l2,
            nn.Dropout(p=dropout),
            nn.ReLU(),
        )

        self.mean_linear = nn.Linear(hidden[1], self.action_space)
        self.log_std_linear = nn.Linear(hidden[1], self.action_space)

        # Overall reward and loss history
        self.reward_history = []
        self.loss_history = []
        self.reset()

    def reset(self):
        # Episode policy and reward history
        self.saved_log_probs = []
        self.rewards = []

    def init_weights(self, init_w=3e-3):
        """
        Initialize weights with xaiver uniform, and
        fill bias with 1 over n
        """
        over_n = 1 / (self.state_space + self.action_space)
        nn.init.xavier_uniform_(self.l1.weight)
        self.l1.bias.data.fill_(over_n)
        nn.init.xavier_uniform_(self.l2.weight)
        self.l2.bias.data.fill_(1 / self.hidden[0])

        self.mean_linear.weight.data.uniform_(-init_w, init_w)
        self.mean_linear.bias.data.uniform_(-init_w, init_w)
        self.log_std_linear.weight.data.uniform_(-init_w, init_w)
        self.log_std_linear.bias.data.uniform_(-init_w, init_w)

    def forward(self, state):
        x = self.ffn(state)
        mean = self.mean_linear(x)
        log_std = self.log_std_linear(x)
        log_std = torch.clamp(
            log_std,
            min=self.log_std_min,
            max=self.log_std_max,
        )

        return mean, log_std

    def evaluate(self, state, epsilon=1e-6):
        """
        Evaluate a state, returning action, log probs,
        mean, log_std, and z, the sampled action
        """
        mean, log_std = self.forward(state)
        std = log_std.exp()

        normal = Normal(mean, std)
        z = normal.sample()
        action = torch.tanh(z)

        log_prob = normal.log_prob(z) - torch.log(1 - action.pow(2) + epsilon)
        log_prob = log_prob.sum(-1, keepdim=True)

        return action, log_prob, z, mean, log_std

    def get_action(self, state):
        """
        Returns an action given a state
        """
        state = torch.FloatTensor(state).unsqueeze(0).to(self.device())
        mean, log_std = self.forward(state)
        std = log_std.exp()

        normal = Normal(mean, std)
        z = normal.sample()
        action = torch.tanh(z)

        action = action.detach().cpu().numpy()
        return action[0]

    def device(self):
        return next(self.parameters()).device


class DiscreteActor(nn.Module):
    def __init__(
        self,
        env,
        hidden=[128, 128],
        dropout=0.0,
        log_std_min=-20,
        log_std_max=2,
    ):
        super(DiscreteActor, self).__init__()
        self.state_space = env.observation_space.shape[0]
        self.action_space = env.action_space.n
        self.hidden = hidden

        self.l1 = nn.Linear(self.state_space, hidden[0])
        self.l2 = nn.Linear(hidden[0], hidden[1])
        self.l3 = nn.Linear(hidden[1], self.action_space)
        self.ffn = nn.Sequential(
            self.l1,
            nn.Dropout(p=dropout),
            nn.ReLU(),
            self.l2,
            nn.Dropout(p=dropout),
            nn.ReLU(),
            self.l3,
            nn.Softmax(dim=-1),
        )

        # Overall reward and loss history
        self.reward_history = []
        self.loss_history = []
        self.reset()

    def reset(self):
        # Episode policy and reward history
        self.saved_log_probs = []
        self.rewards = []

    def init_weights(self, init_w=3e-3):
        """
        Initialize weights with xaiver uniform, and
        fill bias with 1 over n
        """
        over_n = 1 / (self.state_space + self.action_space)
        nn.init.xavier_uniform_(self.l1.weight)
        self.l1.bias.data.fill_(over_n)
        nn.init.xavier_uniform_(self.l2.weight)
        self.l2.bias.data.fill_(1 / self.hidden[0])
        self.l3.weight.data.uniform_(-init_w, init_w)
        self.l3.bias.data.uniform_(-init_w, init_w)

    def forward(self, state):
        return self.ffn(state)

    def evaluate(self, state, epsilon=1e-6, reparam=False):
        """
        Evaluate a state, returning action, log probs,
        mean, log_std, and z, the sampled action
        """

        action_probs = self.forward(state)
        action_pd = GumbelSoftmax(probs=action_probs, temperature=0.9)
        actions = action_pd.rsample() if reparam else action_pd.sample()
        log_probs = action_pd.log_prob(actions)
        return actions, log_probs, None, None, None

    def get_action(self, state):
        """
        Returns an action given a state
        """
        action_probs = self.forward(state)
        action = torch.distributions.Categorical(probs=action_probs).sample()
        action = action.detach().cpu().numpy()
        return action

    def device(self):
        return next(self.parameters()).device


class Policy(nn.Module):
    def __init__(self, env):
        super(Policy, self).__init__()
        state_space = env.observation_space.shape[0]
        action_space = env.action_space.n
        num_hidden = 128

        self.l1 = nn.Linear(state_space, num_hidden, bias=False)
        self.l2 = nn.Linear(num_hidden, action_space, bias=False)

        # Overall reward and loss history
        self.reward_history = []
        self.loss_history = []
        self.reset()

    def reset(self):
        # Episode policy and reward history
        self.saved_log_probs = []
        self.rewards = []

    def forward(self, x):
        model = torch.nn.Sequential(
            self.l1, nn.Dropout(p=0.5), nn.ReLU(), self.l2, nn.Softmax(dim=-1)
        )
        return model(x)

    def device(self):
        if next(self.parameters()).is_cuda:
            return torch.device("cuda")
        else:
            return torch.device("cpu")

    def predict(self, state):
        # Select an action (0 or 1) by running policy model
        # and choosing based on the probabilities in state
        device = self.device()
        state = torch.from_numpy(state).type(torch.FloatTensor).to(device)
        action_probs = self(state)
        distribution = Categorical(action_probs)
        action = distribution.sample()

        # Add log probability of our chosen action to our history
        self.saved_log_probs.append(distribution.log_prob(action))

        return action


class Value(nn.Module):
    def __init__(self, env):
        super(Value, self).__init__()
        state_space = env.observation_space.shape[0]
        num_hidden = 128

        self.l1 = nn.Linear(state_space, num_hidden, bias=False)
        self.l2 = nn.Linear(num_hidden, 1, bias=False)

    def forward(self, x):
        model = torch.nn.Sequential(
            self.l1,
            nn.Dropout(p=0.5),
            nn.ReLU(),
            self.l2,
        )
        return model(x)


def save_model(model):
    # if isinstance(model, Planner):
    #     return save(
    #       model.state_dict(), path.join(path.dirname(path.abspath(__file__)),
    #       'planner.th')
    #     )
    raise ValueError("model type '%s' not supported!" % str(type(model)))


def load_model(model):
    r = None
    # if isinstance(model, Planner):
    #     r = Planner()
    #     r.load_state_dict(load(
    #         path.join(path.dirname(path.abspath(__file__)), 'planner.th'),
    #         map_location=model.device)
    #     )
    return r
