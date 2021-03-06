import copy
import time
from collections import OrderedDict, deque
from logging import getLogger

import cv2
import gym
import numpy as np
import torch
from gym.wrappers import Monitor
from gym.wrappers.monitoring.stats_recorder import StatsRecorder
from gym import spaces
import minerl

from .util import apply_rec_to_dict, apply_to_state, apply_to_state_list

cv2.ocl.setUseOpenCL(False)
logger = getLogger(__name__)


def add_to_set_in_dict(dict_to_add, name, val):
    try:
        dict_to_add[name].add(val)
    except KeyError:
        dict_to_add[name] = {val}


def map2closest_val(val, number_list):
    return min(number_list, key=lambda x: abs(x - val))


class FrameStack(gym.Wrapper):
    def __init__(self, env, k, store_stacked, stack_dim=0):
        """Stack k last frames.
        Returns lazy array, which is much more memory efficient.
        See Also
        --------
        baselines.common.atari_wrappers.LazyFrames
        """
        gym.Wrapper.__init__(self, env)
        self.k = k
        self.frames = deque([], maxlen=k)
        self.stack_dim = stack_dim
        self.store_stacked = store_stacked

        if isinstance(env.observation_space, dict) or isinstance(env.observation_space, minerl.env.spaces.Dict):
            new_space = apply_rec_to_dict(self.transform_obs_space, env.observation_space)
            self.observation_space = ItObsDict(new_space)
        else:
            self.observation_space = self.transform_obs_space(self.observation_space)

        # The first dim of obs is the batch_size, skip it:
        self.stack_dim += 1

    def transform_obs_space(self, obs_space):
        shp = obs_space.shape
        stack_dim = self.stack_dim
        if stack_dim == -1:
            stack_dim = len(shp) - 1
        shp = [size * self.k if idx == stack_dim else size for idx, size in enumerate(shp)]
        obs_space = spaces.Box(low=0, high=255, shape=shp, dtype=obs_space.dtype)
        return obs_space

    def reset(self):
        ob = self.env.reset()
        for _ in range(self.k):
            self.frames.append(ob)
        return self._get_ob()

    def step(self, action):
        ob, reward, done, info = self.env.step(action)
        self.frames.append(ob)
        return self._get_ob(), reward, done, info

    def _get_ob(self):
        assert len(self.frames) == self.k
        return LazyFrames(list(self.frames), self.stack_dim, self.store_stacked)


class LazyFrames:
    def __init__(self, frames, stack_dim, store_stacked):
        """This object ensures that common frames between the observations are only stored once.
        It exists purely to optimize memory usage which can be huge for DQN's 1M frames replay
        buffers.
        This object should only be converted to numpy array before being passed to the model.
        You'd not believe how complex the previous solution was."""
        self._frames = frames
        self._out = None
        self.obs_is_dict = isinstance(self._frames[0], dict)
        self.stack_dim = stack_dim
        self.store_stacked = store_stacked

    def _force(self):
        if self.store_stacked:
            raise NotImplementedError("Just don't...")
            #if self._out is None:
            #    self._out = self.stack_frames(self._frames)
            #    self._frames = None
            #return self._out
        else:
            return self.stack_frames(self._frames)

    def stack_frames(self, frames):
        obs = apply_to_state_list(self.stack, frames)
        return obs
        #if self.obs_is_dict:
        #    obs = {
        #        self.stack([frame[key] for frame in frames])
        #        for key in frames[0]
        #    }
        #else:
        #    obs = self.stack(frames)
        #return obs

    def stack(self, frames):
        return torch.cat(list(frames), dim=self.stack_dim)

    def make_state(self):
        return self._force()

    def __array__(self, dtype=None):
        print("Access forbidden array")
        out = self._force()
        if dtype is not None:
            out = out.type(dtype)
        return out

    def __len__(self):
        print("Access forbidden len")
        return len(self._force())

    def __getitem__(self, i):
        print("Access forbidden getitem")
        #return self._force()[i]
        return self._frames[i]

    def count(self):
        print("Access forbidden count")
        frames = self._force()
        return frames.shape[frames.ndim - 1]

    def frame(self, i):
        print("Access forbidden frame")
        return self._force()[..., i]


class HierarchicalActionWrapper(gym.ActionWrapper):
    """Convert MineRL env's `Dict` action space as a serial discrete action space.

    The term "serial" means that this wrapper can only push one key at each step.
    "attack" action will be alwarys triggered.

    Parameters
    ----------
    env
        Wrapping gym environment.
    always_keys
        List of action keys, which should be always pressed throughout interaction with environment.
        If specified, the "noop" action is also affected.
    reverse_keys
        List of action keys, which should be always pressed but can be turn off via action.
        If specified, the "noop" action is also affected.
    exclude_keys
        List of action keys, which should be ignored for discretizing action space.
    exclude_noop
        The "noop" will be excluded from discrete action list.
    """

    BINARY_KEYS = ['forward', 'back', 'left', 'right', 'jump', 'sneak', 'sprint', 'attack']

    def __init__(self, env, always_keys=None, reverse_keys=None, exclude_keys=None, exclude_noop=True, env_name=""):
        super().__init__(env)

        self.always_keys = [] if always_keys is None else always_keys
        self.reverse_keys = [] if reverse_keys is None else reverse_keys
        self.exclude_keys = [] if exclude_keys is None else exclude_keys
        if len(set(self.always_keys) | set(self.reverse_keys) | set(self.exclude_keys)) != \
                len(self.always_keys) + len(self.reverse_keys) + len(self.exclude_keys):
            raise ValueError('always_keys ({}) or reverse_keys ({}) or exclude_keys ({}) intersect each other.'.format(
                    self.always_keys, self.reverse_keys, self.exclude_keys))
        self.exclude_noop = exclude_noop

        self.wrapping_action_space = self.env.action_space
        self._noop_template = OrderedDict([
            ('forward', 0),
            ('back', 0),
            ('left', 0),
            ('right', 0),
            ('jump', 0),
            ('sneak', 0),
            ('sprint', 0),
            ('attack', 0),
            ('camera', np.zeros((2,), dtype=np.float32)),
            # 'none', 'dirt' (Obtain*:)+ 'stone', 'cobblestone', 'crafting_table', 'furnace', 'torch'
            ('place', 0),
            # (Obtain* tasks only) 'none', 'wooden_axe', 'wooden_pickaxe', 'stone_axe', 'stone_pickaxe', 'iron_axe', 'iron_pickaxe'
            ('equip', 0),
            # (Obtain* tasks only) 'none', 'torch', 'stick', 'planks', 'crafting_table'
            ('craft', 0),
            # (Obtain* tasks only) 'none', 'wooden_axe', 'wooden_pickaxe', 'stone_axe', 'stone_pickaxe', 'iron_axe', 'iron_pickaxe', 'furnace'
            ('nearbyCraft', 0),
            # (Obtain* tasks only) 'none', 'iron_ingot', 'coal'
            ('nearbySmelt', 0),
        ])
        for key, space in self.wrapping_action_space.spaces.items():
            if key not in self._noop_template:
                raise ValueError('Unknown action name: {}'.format(key))

        # get noop
        self.noop = copy.deepcopy(self._noop_template)
        for key in self._noop_template:
            if key not in self.wrapping_action_space.spaces:
                del self.noop[key]

        # check&set always_keys
        for key in self.always_keys:
            if key not in self.BINARY_KEYS:
                raise ValueError('{} is not allowed for `always_keys`.'.format(key))
            self.noop[key] = 1
        logger.info('always pressing keys: {}'.format(self.always_keys))
        # check&set reverse_keys
        for key in self.reverse_keys:
            if key not in self.BINARY_KEYS:
                raise ValueError('{} is not allowed for `reverse_keys`.'.format(key))
            self.noop[key] = 1
        logger.info('reversed pressing keys: {}'.format(self.reverse_keys))
        # check exclude_keys
        for key in self.exclude_keys:
            if key not in self.noop:
                raise ValueError('unknown exclude_keys: {}'.format(key))
        logger.info('always ignored keys: {}'.format(self.exclude_keys))

        # get each discrete action
        self._actions = [self.noop]
        idx = 0

        self.word2idx_set = {}

        self.dict2id_set = {}

        self.lateral_options = ["left", "none_lateral", "right"]
        self.straight_options = ["back", "none_straight", "forward"]
        self.attack_options = ["none_attack", "attack"]
        self.jump_options = ["none_jump", "jump"]
        self.camera_x_options = [-10, 0, 10]  # [-10, -5, -1, 0, 1, 5, 10]
        self.camera_y_options = self.camera_x_options
        self.camera_x_options_string = ["x_" + str(number) for number in self.camera_x_options]
        self.camera_y_options_string = ["y_" + str(number) for number in self.camera_x_options]
        self.num_camera_actions = len(self.camera_x_options)

        if env_name == "MineRLTreechop-v0":
            self.attack_options = ["attack"]
            self.straight_options = ["forward"]
            self.lateral_options = ["none_lateral"]

        # Create move possibilities:
        for lateral in self.lateral_options:
            for straight in self.straight_options:
                for attack in self.attack_options:
                    for jump in self.jump_options:
                        for camera_x in self.camera_x_options:
                            for camera_y in self.camera_y_options:
                                # To later on map from dict to idx:
                                # add_to_set_in_dict(self.word2idx_set, lateral, idx)
                                # add_to_set_in_dict(self.word2idx_set, straight, idx)
                                # add_to_set_in_dict(self.word2idx_set, attack, idx)
                                # add_to_set_in_dict(self.word2idx_set, jump, idx)
                                # camera_name = "camera_" + str(camera_x) + "_" + str(camera_y)
                                # add_to_set_in_dict(self.word2idx_set, camera_name, idx)

                                # Create op that maps from idx to dict:
                                op = copy.deepcopy(self.noop)
                                if lateral != "none_lateral":
                                    op[lateral] = 1
                                if straight != "none_straight":
                                    op[straight] = 1
                                if attack != "none_attack":
                                    op[attack] = 1
                                if jump != "none_jump":
                                    op[jump] = 1
                                op["camera"] = (camera_x, camera_y)
                                # For idx to dict:
                                self._actions.append(op)
                                # For dict to idx:
                                self.dict2id_set[tuple(sorted(op.items()))] = idx

                                idx += 1

        # Create place, equip, craft etc options:
        for key in self.noop:
            if key in self.always_keys or key in self.exclude_keys:
                continue
            if key in {'place', 'equip', 'craft', 'nearbyCraft', 'nearbySmelt'}:
                # action candidate : {1, 2, ..., len(space)-1}  (0 is ignored because it is for noop)
                for a in range(1, self.wrapping_action_space.spaces[key].n):
                    # Dict to Idx:
                    name = key + str(a)
                    add_to_set_in_dict(self.word2idx_set, name, idx)

                    # Idx to dict:
                    op = copy.deepcopy(self.noop)
                    op[key] = a
                    self._actions.append(op)

                    op["camera"] = tuple(op["camera"])
                    self.dict2id_set[tuple(sorted(op.items()))] = idx

                    idx += 1
            else:
                continue
        if self.exclude_noop:
            del self._actions[0]
        n = len(self._actions)
        self.n = n
        self.action_space = gym.spaces.Discrete(n)
        logger.info('{} is converted to {}.'.format(self.wrapping_action_space, self.action_space))

    def action(self, action):
        if not self.action_space.contains(action):
            raise ValueError('action {} is invalid for {}'.format(action, self.action_space))

        original_space_action = self._actions[action]
        logger.debug('discrete action {} -> original action {}'.format(action, original_space_action))
        return original_space_action

    def dicts2idxs(self, action_dict_iterable):
        return [self.dict2idx(action_dict) for action_dict in action_dict_iterable]

    def dict2idx(self, action_dict):
        action_dict_copy = action_dict.copy()
        x = action_dict["camera"][0]
        y = action_dict["camera"][1]
        mapped_camera = (map2closest_val(x, self.camera_x_options), map2closest_val(y, self.camera_y_options))
        action_dict_copy["camera"] = mapped_camera
        return self.dict2id_set[tuple(sorted(action_dict_copy.items()))]

    def dict2idx_old(self, action_dict):
        possible_idxs = None
        for key in action_dict:
            val = action_dict[key]
            name = key
            # For camera:
            if key == "camera":
                x = map2closest_val(val[0], self.camera_x_options)
                y = map2closest_val(val[1], self.camera_y_options)
                name += "_" + str(x) + "_" + str(y)
            # For other keys:
            elif key in ('place', 'equip', 'craft', 'nearbyCraft', 'nearbySmelt'):
                # Only one idx per place value, so it is easy to match:
                if val:
                    name = key + str(val)
                    idx = self.word2idx_set[name][0]
                    return idx
            else:
                if val:
                    name = key
                else:
                    if key == "left" or key == "right":
                        name = "none_lateral"
                    elif key == "forward" or key == "backward":
                        name = "none_straight"
                    elif key == "jump":
                        name = "none_jump"
                    elif key == "attack":
                        name = "none_attack"
                new_idxs = self.word2idx_set[name]
                # Repeatedly take the intersection of possible values until only one idx is left:
                if possible_idxs is None:
                    possible_idxs = new_idxs
                else:
                    possible_idxs = possible_idxs.intersection(new_idxs)
                    # If only one more is possible:
                    if len(possible_idxs) == 1:
                        return possible_idxs[0]


class FrameSkip(gym.Wrapper):
    """Return every `skip`-th frame and repeat given action during skip.

    Note that this wrapper does not "maximize" over the skipped frames.
    """

    def __init__(self, env, skip=4):
        super().__init__(env)

        self._skip = skip

    def step(self, action):
        total_reward = 0.0
        for _ in range(self._skip):
            obs, reward, done, info = self.env.step(action)
            total_reward += reward
            if done:
                break
        return obs, total_reward, done, info


class ObtainPoVWrapper(gym.ObservationWrapper):
    """Obtain 'pov' value (current game display) of the original observation."""

    def __init__(self, env):
        super().__init__(env)

        self.observation_space = self.env.observation_space.spaces['pov']

    def observation(self, observation):
        return observation['pov']


# def one_hot_encode(x, num_actions):
#    y = torch.zeros(x.shape[0], num_actions).float()
#    return y.scatter(1, x, 1)

def one_hot_encode_single(number, num_actions):
    y = torch.zeros(num_actions)
    y[number] = 1.0
    return y


def process_equipped(mainhand_dict):
    dmg = torch.tensor(mainhand_dict["damage"], dtype=torch.float).unsqueeze(0)
    max_dmg = torch.tensor(mainhand_dict["maxDamage"], dtype=torch.float).unsqueeze(0)
    obj_type = mainhand_dict["type"]
    if np.any(obj_type != 0):
        possible_non_none_types = ("air", "wooden_axe", "wooden_pickaxe", "stone_axe", "stone_pickaxe", "iron_axe",
                                   "iron_pickaxe")
        if obj_type != 0 and obj_type not in range(1, 8) and obj_type not in possible_non_none_types:
            obj_type = 8
    item_type = one_hot_encode_single(obj_type, 9)
    return torch.cat([dmg, max_dmg, item_type], dim=0)


class DefaultWrapper(gym.ObservationWrapper):
    def __init__(self, env, rgb2gray):
        super().__init__(env)

    def observation(self, observation):
        obs = torch.from_numpy(observation).float().unsqueeze(0)
        return obs


class ItObsDict(minerl.env.spaces.Dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def __iter__(self):
        for key in self.spaces:
            yield key
            
    def keys(self):
        return self.spaces.keys()
    
    def values(self):
        return self.spaces.values()


class Convert2TorchWrapper(gym.ObservationWrapper):
    def __init__(self, env, rgb2gray):
        super().__init__(env)
        self.rgb2gray = rgb2gray

        sample = env.observation_space.sample()

        changed_sampled = apply_rec_to_dict(lambda x: x.squeeze(0), self.observation(sample))

        new_space = apply_rec_to_dict(lambda x: spaces.Box(low=0, high=255, shape=x.shape, dtype=np.float),
                                      changed_sampled)

        self.observation_space = ItObsDict(new_space)

    def observation(self, obs_dict, expert_data=False):
        new_obs = {}
        for key in obs_dict:
            if key == "equipped_items":
                mainhand_dict = obs_dict[key]["mainhand"]
                obs = process_equipped(mainhand_dict)
                # obs = torch.cat(
                #    [torch.from_numpy(process_equipped(equip_dict["mainhand"])) for equip_dict in equip_dict_list])
            elif key == "inventory":
                inv_dict = obs_dict[key]
                # obs = torch.cat([torch.from_numpy(process_inv(inv_dict)).float() for inv_dict in inv_dict_list])
                obs = torch.cat(
                        [torch.from_numpy(np.ascontiguousarray(inv_dict[key])) for key in inv_dict])
            elif key == "pov":
                obs = torch.from_numpy(np.ascontiguousarray(obs_dict[key]))
                if self.rgb2gray:
                    obs = np.round(obs.float().mean(dim=-1)).unsqueeze(0)
                else:
                    obs = obs.permute(2, 0, 1)
                obs = obs.byte().clone()
            elif key == "compassAngle":
                obs = torch.tensor(obs_dict[key], dtype=torch.float).unsqueeze(0)
            else:
                print("Unknown dict key: ", key)
                raise NotImplementedError
            if expert_data:
                obs = obs.squeeze().unsqueeze(0)
            new_obs[key] = obs.unsqueeze(0)

        return new_obs


class AtariObsWrapper(gym.ObservationWrapper):
    def __init__(self, env, rgb2gray):
        super().__init__(env)
        self.rgb2gray = rgb2gray
        self.last_obs = None
        self.observation_space = spaces.Box(low=0, high=255, shape=(1, 80, 80), dtype=env.observation_space.dtype)

    def observation(self, obs):
        obs = torch.from_numpy(np.ascontiguousarray(obs))
        obs = obs[35:195]
        obs = obs[::2, ::2]
        if self.last_obs:
            obs = torch.max(self.last_obs, obs)  # kill object flickering

        if self.rgb2gray:
            obs = np.round(obs.float().mean(dim=-1)).unsqueeze(0)
        else:
            obs.permute(2, 0, 1)
        obs = obs.byte().clone().unsqueeze(0)

        return obs

    def reset(self):
        self.last_obs = None
        return super().reset()


class PoVWithCompassAngleWrapper(gym.ObservationWrapper):
    """Take 'pov' value (current game display) and concatenate compass angle information with it, as a new channel of image;
    resulting image has RGB+compass (or K+compass for gray-scaled image) channels.
    """

    def __init__(self, env):
        super().__init__(env)

        self._compass_angle_scale = 180 / 255  # NOTE: `ScaledFloatFrame` will scale the pixel values with 255.0 later

        pov_space = self.env.observation_space.spaces['pov']
        compass_angle_space = self.env.observation_space.spaces['compassAngle']

        low = self.observation({'pov': pov_space.low, 'compassAngle': compass_angle_space.low})
        high = self.observation({'pov': pov_space.high, 'compassAngle': compass_angle_space.high})

        self.observation_space = gym.spaces.Box(low=low, high=high)

    def observation(self, observation):
        pov = observation['pov']
        compass_scaled = observation['compassAngle'] / self._compass_angle_scale
        compass_channel = np.ones(shape=list(pov.shape[:-1]) + [1], dtype=pov.dtype) * compass_scaled
        return np.concatenate([pov, compass_channel], axis=-1)


class MoveAxisWrapper(gym.ObservationWrapper):
    """Move axes of observation ndarrays."""

    def __init__(self, env, source, destination):
        assert isinstance(env.observation_space, gym.spaces.Box)
        super().__init__(env)

        self.source = source
        self.destination = destination

        low = self.observation(self.observation_space.low)
        high = self.observation(self.observation_space.high)
        self.observation_space = gym.spaces.Box(low=low, high=high, dtype=self.observation_space.dtype)

    def observation(self, frame):
        return np.moveaxis(frame, self.source, self.destination)


class GrayScaleWrapper(gym.ObservationWrapper):
    def __init__(self, env, dict_space_key=None):
        super().__init__(env)

        self._key = dict_space_key

        if self._key is None:
            original_space = self.observation_space
        else:
            original_space = self.observation_space.spaces[self._key]
        height, width = original_space.shape[0], original_space.shape[1]

        # sanity checks
        ideal_image_space = gym.spaces.Box(low=0, high=255, shape=(height, width, 3), dtype=np.uint8)
        if original_space != ideal_image_space:
            raise ValueError('Image space should be {}, but given {}.'.format(ideal_image_space, original_space))
        if original_space.dtype != np.uint8:
            raise ValueError('Image should `np.uint8` typed, but given {}.'.format(original_space.dtype))

        height, width = original_space.shape[0], original_space.shape[1]
        new_space = gym.spaces.Box(low=0, high=255, shape=(height, width, 1), dtype=np.uint8)
        if self._key is None:
            self.observation_space = new_space
        else:
            self.observation_space.spaces[self._key] = new_space

    def observation(self, obs):
        if self._key is None:
            frame = obs
        else:
            frame = obs[self._key]
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        frame = np.expand_dims(frame, -1)
        if self._key is None:
            obs = frame
        else:
            obs[self._key] = frame
        return obs


class SerialDiscreteActionWrapper(gym.ActionWrapper):
    """Convert MineRL env's `Dict` action space as a serial discrete action space.

    The term "serial" means that this wrapper can only push one key at each step.
    "attack" action will be alwarys triggered.

    Parameters
    ----------
    env
        Wrapping gym environment.
    always_keys
        List of action keys, which should be always pressed throughout interaction with environment.
        If specified, the "noop" action is also affected.
    reverse_keys
        List of action keys, which should be always pressed but can be turn off via action.
        If specified, the "noop" action is also affected.
    exclude_keys
        List of action keys, which should be ignored for discretizing action space.
    exclude_noop
        The "noop" will be excluded from discrete action list.
    """

    BINARY_KEYS = ['forward', 'back', 'left', 'right', 'jump', 'sneak', 'sprint', 'attack']

    def __init__(self, env, always_keys=None, reverse_keys=None, exclude_keys=None, exclude_noop=False,
                 forward_when_jump=False):
        super().__init__(env)

        self.always_keys = [] if always_keys is None else always_keys
        self.reverse_keys = [] if reverse_keys is None else reverse_keys
        self.exclude_keys = [] if exclude_keys is None else exclude_keys
        if len(set(self.always_keys) | set(self.reverse_keys) | set(self.exclude_keys)) != \
                len(self.always_keys) + len(self.reverse_keys) + len(self.exclude_keys):
            raise ValueError('always_keys ({}) or reverse_keys ({}) or exclude_keys ({}) intersect each other.'.format(
                    self.always_keys, self.reverse_keys, self.exclude_keys))
        self.exclude_noop = exclude_noop

        self.wrapping_action_space = self.env.action_space
        self._noop_template = OrderedDict([
            ('forward', 0),
            ('back', 0),
            ('left', 0),
            ('right', 0),
            ('jump', 0),
            ('sneak', 0),
            ('sprint', 0),
            ('attack', 0),
            ('camera', np.zeros((2,), dtype=np.float32)),
            # 'none', 'dirt' (Obtain*:)+ 'stone', 'cobblestone', 'crafting_table', 'furnace', 'torch'
            ('place', 0),
            # (Obtain* tasks only) 'none', 'wooden_axe', 'wooden_pickaxe', 'stone_axe', 'stone_pickaxe', 'iron_axe', 'iron_pickaxe'
            ('equip', 0),
            # (Obtain* tasks only) 'none', 'torch', 'stick', 'planks', 'crafting_table'
            ('craft', 0),
            # (Obtain* tasks only) 'none', 'wooden_axe', 'wooden_pickaxe', 'stone_axe', 'stone_pickaxe', 'iron_axe', 'iron_pickaxe', 'furnace'
            ('nearbyCraft', 0),
            # (Obtain* tasks only) 'none', 'iron_ingot', 'coal'
            ('nearbySmelt', 0),
        ])
        for key, space in self.wrapping_action_space.spaces.items():
            if key not in self._noop_template:
                raise ValueError('Unknown action name: {}'.format(key))

        # get noop
        self.noop = copy.deepcopy(self._noop_template)
        for key in self._noop_template:
            if key not in self.wrapping_action_space.spaces:
                del self.noop[key]

        # check&set always_keys
        for key in self.always_keys:
            if key not in self.BINARY_KEYS:
                raise ValueError('{} is not allowed for `always_keys`.'.format(key))
            self.noop[key] = 1
        logger.info('always pressing keys: {}'.format(self.always_keys))
        # check&set reverse_keys
        for key in self.reverse_keys:
            if key not in self.BINARY_KEYS:
                raise ValueError('{} is not allowed for `reverse_keys`.'.format(key))
            self.noop[key] = 1
        logger.info('reversed pressing keys: {}'.format(self.reverse_keys))
        # check exclude_keys
        for key in self.exclude_keys:
            if key not in self.noop:
                raise ValueError('unknown exclude_keys: {}'.format(key))
        logger.info('always ignored keys: {}'.format(self.exclude_keys))

        # get each discrete action
        self._actions = [self.noop]
        for key in self.noop:
            if key in self.always_keys or key in self.exclude_keys:
                continue
            if key in self.BINARY_KEYS:
                # action candidate : {1}  (0 is ignored because it is for noop), or {0} when `reverse_keys`.
                op = copy.deepcopy(self.noop)
                if key in self.reverse_keys:
                    op[key] = 0
                else:
                    op[key] = 1
                if key == "jump" and forward_when_jump:
                    op["forward"] = 1
                self._actions.append(op)
            elif key == 'camera':
                # action candidate : {[0, -10], [0, 10]}
                op = copy.deepcopy(self.noop)
                op[key] = np.array([0, -10], dtype=np.float32)
                self._actions.append(op)
                op = copy.deepcopy(self.noop)
                op[key] = np.array([0, 10], dtype=np.float32)
                self._actions.append(op)
            elif key in {'place', 'equip', 'craft', 'nearbyCraft', 'nearbySmelt'}:
                # action candidate : {1, 2, ..., len(space)-1}  (0 is ignored because it is for noop)
                for a in range(1, self.wrapping_action_space.spaces[key].n):
                    op = copy.deepcopy(self.noop)
                    op[key] = a
                    self._actions.append(op)
            print(key, op[key])
        if self.exclude_noop:
            del self._actions[0]
        n = len(self._actions)
        self.action_space = gym.spaces.Discrete(n)
        logger.info('{} is converted to {}.'.format(self.wrapping_action_space, self.action_space))

    def action(self, action):
        if not self.action_space.contains(action):
            raise ValueError('action {} is invalid for {}'.format(action, self.action_space))

        original_space_action = self._actions[action]
        logger.debug('discrete action {} -> original action {}'.format(action, original_space_action))
        return original_space_action


class CombineActionWrapper(gym.ActionWrapper):
    """Combine MineRL env's "exclusive" actions.

    "exclusive" actions will be combined as:
        - "forward", "back" -> noop/forward/back (Discrete(3))
        - "left", "right" -> noop/left/right (Discrete(3))
        - "sneak", "sprint" -> noop/sneak/sprint (Discrete(3))
        - "attack", "place", "equip", "craft", "nearbyCraft", "nearbySmelt"
            -> noop/attack/place/equip/craft/nearbyCraft/nearbySmelt (Discrete(n))
    The combined action's names will be concatenation of originals, i.e.,
    "forward_back", "left_right", "snaek_sprint", "attack_place_equip_craft_nearbyCraft_nearbySmelt".
    """

    def __init__(self, env):
        super().__init__(env)

        self.wrapping_action_space = self.env.action_space

        def combine_exclusive_actions(keys):
            """
            Dict({'forward': Discrete(2), 'back': Discrete(2)})
            =>
            new_actions: [{'forward':0, 'back':0}, {'forward':1, 'back':0}, {'forward':0, 'back':1}]
            """
            new_key = '_'.join(keys)
            valid_action_keys = [k for k in keys if k in self.wrapping_action_space.spaces]
            noop = {a: 0 for a in valid_action_keys}
            new_actions = [noop]

            for key in valid_action_keys:
                space = self.wrapping_action_space.spaces[key]
                for i in range(1, space.n):
                    op = copy.deepcopy(noop)
                    op[key] = i
                    new_actions.append(op)
            return new_key, new_actions

        self._maps = {}
        for keys in (
                ('forward', 'back'), ('left', 'right'), ('sneak', 'sprint'),
                ('attack', 'place', 'equip', 'craft', 'nearbyCraft', 'nearbySmelt')):
            new_key, new_actions = combine_exclusive_actions(keys)
            self._maps[new_key] = new_actions

        self.noop = OrderedDict([
            ('forward_back', 0),
            ('left_right', 0),
            ('jump', 0),
            ('sneak_sprint', 0),
            ('camera', np.zeros((2,), dtype=np.float32)),
            ('attack_place_equip_craft_nearbyCraft_nearbySmelt', 0),
        ])

        self.action_space = gym.spaces.Dict({
            'forward_back':
                gym.spaces.Discrete(len(self._maps['forward_back'])),
            'left_right':
                gym.spaces.Discrete(len(self._maps['left_right'])),
            'jump':
                self.wrapping_action_space.spaces['jump'],
            'sneak_sprint':
                gym.spaces.Discrete(len(self._maps['sneak_sprint'])),
            'camera':
                self.wrapping_action_space.spaces['camera'],
            'attack_place_equip_craft_nearbyCraft_nearbySmelt':
                gym.spaces.Discrete(len(self._maps['attack_place_equip_craft_nearbyCraft_nearbySmelt']))
        })

        logger.info('{} is converted to {}.'.format(self.wrapping_action_space, self.action_space))
        for k, v in self._maps.items():
            logger.info('{} -> {}'.format(k, v))

    def action(self, action):
        if not self.action_space.contains(action):
            raise ValueError('action {} is invalid for {}'.format(action, self.action_space))

        original_space_action = OrderedDict()
        for k, v in action.items():
            if k in self._maps:
                a = self._maps[k][v]
                original_space_action.update(a)
            else:
                original_space_action[k] = v

        logger.debug('action {} -> original action {}'.format(action, original_space_action))
        return original_space_action


class SerialDiscreteCombineActionWrapper(gym.ActionWrapper):
    def __init__(self, env):
        super().__init__(env)

        self.wrapping_action_space = self.env.action_space

        self.noop = OrderedDict([
            ('forward_back', 0),
            ('left_right', 0),
            ('jump', 0),
            ('sneak_sprint', 0),
            ('camera', np.zeros((2,), dtype=np.float32)),
            ('attack_place_equip_craft_nearbyCraft_nearbySmelt', 0),
        ])

        # get each discrete action
        self._actions = [self.noop]
        for key in self.noop:
            if key == 'camera':
                # action candidate : {[0, -10], [0, 10]}
                op = copy.deepcopy(self.noop)
                op[key] = np.array([0, -10], dtype=np.float32)
                self._actions.append(op)
                op = copy.deepcopy(self.noop)
                op[key] = np.array([0, 10], dtype=np.float32)
                self._actions.append(op)
            else:
                for a in range(1, self.wrapping_action_space.spaces[key].n):
                    op = copy.deepcopy(self.noop)
                    op[key] = a
                    self._actions.append(op)

        n = len(self._actions)
        self.action_space = gym.spaces.Discrete(n)
        logger.info('{} is converted to {}.'.format(self.wrapping_action_space, self.action_space))

    def action(self, action):
        if not self.action_space.contains(action):
            raise ValueError('action {} is invalid for {}'.format(action, self.action_space))

        original_space_action = self._actions[action]
        logger.debug('discrete action {} -> original action {}'.format(action, original_space_action))
        return original_space_action
