#!/usr/bin/env python3
"""                          _              _                           
  _ __   ___ _   _ _ __ __ _| |   ___ _ __ | |__   __ _ _ __   ___ ___  
 | '_ \ / _ \ | | | '__/ _` | |  / _ \ '_ \| '_ \ / _` | '_ \ / __/ _ \ 
 | | | |  __/ |_| | | | (_| | | |  __/ | | | | | | (_| | | | | (_|  __/ 
 |_| |_|\___|\__,_|_|  \__,_|_|  \___|_| |_|_| |_|\__,_|_| |_|\___\___| 

"""
#
# Copyright (c) 2016, Alex J. Champandard.
#
# Neural Enhance is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License version 3. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#

__version__ = '0.1'

import os
import sys
import bz2
import glob
import math
import time
import pickle
import random
import argparse
import itertools
import threading
import collections


# Configure all options first so we can later custom-load other libraries (Theano) based on device specified by user.
parser = argparse.ArgumentParser(description='Generate a new image by applying style onto a content image.',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
add_arg = parser.add_argument
add_arg('files',                nargs='*', default=[])
add_arg('--scales',             default=2, type=int,                help='How many times to perform 2x upsampling.')
add_arg('--model',              default='small', type=str,          help='Name of the neural network to load/save.')
add_arg('--train',              default=False, type=str,            help='File pattern to load for training.')
add_arg('--batch-resolution',   default=192, type=int,              help='Resolution of images in training batch.')
add_arg('--batch-size',         default=15, type=int,               help='Number of images per training batch.')
add_arg('--buffer-size',        default=1500, type=int,             help='Total image fragments kept in cache.')
add_arg('--buffer-similar',     default=5, type=int,                help='Fragments cached for each image loaded.')
add_arg('--epochs',             default=10, type=int,               help='Total number of iterations in training.')
add_arg('--epoch-size',         default=72, type=int,               help='Number of batches trained in an epoch.')
add_arg('--learning-rate',      default=1E-4, type=float,           help='Parameter for the ADAM optimizer.')
add_arg('--learning-period',    default=50, type=int,               help='How often to decay the learning rate.')
add_arg('--learning-decay',     default=0.5, type=float,            help='How much to decay the learning rate.')
add_arg('--generator-filters',  default=[64], nargs='+', type=int,  help='Number of convolution units in network.')
add_arg('--generator-blocks',   default=4, type=int,                help='Number of residual blocks per iteration.')
add_arg('--generator-residual', default=2, type=int,                help='Number of layers in a residual block.')
add_arg('--perceptual-layer',   default='conv2_2', type=str,        help='Which VGG layer to use as loss component.')
add_arg('--perceptual-weight',  default=1e0, type=float,            help='Weight for VGG-layer perceptual loss.')
add_arg('--discriminator-size', default=32, type=int,               help='Multiplier for number of filters in D.')
add_arg('--smoothness-weight',  default=2e5, type=float,            help='Weight of the total-variation loss.')
add_arg('--adversary-weight',   default=1e2, type=float,            help='Weight of adversarial loss compoment.')
add_arg('--generator-start',    default=0, type=int,                help='Epoch count to start training generator.')
add_arg('--discriminator-start',default=1, type=int,                help='Epoch count to update the discriminator.')
add_arg('--adversarial-start',  default=2, type=int,                help='Epoch for generator to use discriminator.')
add_arg('--device',             default='cpu', type=str,            help='Name of the CPU/GPU to use, for Theano.')
args = parser.parse_args()


#----------------------------------------------------------------------------------------------------------------------

# Color coded output helps visualize the information a little better, plus it looks cool!
class ansi:
    WHITE = '\033[0;97m'
    WHITE_B = '\033[1;97m'
    YELLOW = '\033[0;33m'
    YELLOW_B = '\033[1;33m'
    RED = '\033[0;31m'
    RED_B = '\033[1;31m'
    BLUE = '\033[0;94m'
    BLUE_B = '\033[1;94m'
    CYAN = '\033[0;36m'
    CYAN_B = '\033[1;36m'
    ENDC = '\033[0m'

def error(message, *lines):
    string = "\n{}ERROR: " + message + "{}\n" + "\n".join(lines) + ("{}\n" if lines else "{}")
    print(string.format(ansi.RED_B, ansi.RED, ansi.ENDC))
    sys.exit(-1)

def warn(message, *lines):
    string = "\n{}WARNING: " + message + "{}\n" + "\n".join(lines) + "{}\n"
    print(string.format(ansi.YELLOW_B, ansi.YELLOW, ansi.ENDC))

def extend(lst): return itertools.chain(lst, itertools.repeat(lst[-1]))

print("""{}   {}Super Resolution for images and videos powered by Deep Learning!{}
  - Code licensed as AGPLv3, models under CC BY-NC-SA.{}""".format(ansi.CYAN_B, __doc__, ansi.CYAN, ansi.ENDC))

# Load the underlying deep learning libraries based on the device specified.  If you specify THEANO_FLAGS manually,
# the code assumes you know what you are doing and they are not overriden!
os.environ.setdefault('THEANO_FLAGS', 'floatX=float32,device={},force_device=True,allow_gc=True,'\
                                      'print_active_device=False,lib.cnmem=1.0'.format(args.device))

# Scientific & Imaging Libraries
import numpy as np
import scipy.optimize, scipy.ndimage, scipy.misc

# Numeric Computing (GPU)
import theano
import theano.tensor as T
T.nnet.softminus = lambda x: x - T.nnet.softplus(x)

# Support ansi colors in Windows too.
if sys.platform == 'win32':
    import colorama

# Deep Learning Framework
import lasagne
from lasagne.layers import Conv2DLayer as ConvLayer, Deconv2DLayer as DeconvLayer, Pool2DLayer as PoolLayer
from lasagne.layers import InputLayer, ConcatLayer, ElemwiseSumLayer, batch_norm

print('{}  - Using the device `{}` for neural computation.{}\n'.format(ansi.CYAN, theano.config.device, ansi.ENDC))


#======================================================================================================================
# Image Processing
#======================================================================================================================
class DataLoader(threading.Thread):

    def __init__(self):
        super(DataLoader, self).__init__(daemon=True)
        self.data_ready = threading.Event()
        self.data_copied = threading.Event()

        self.resolution = args.batch_resolution
        self.buffer = np.zeros((args.buffer_size, 3, self.resolution, self.resolution), dtype=np.float32)
        self.files = glob.glob(args.train)
        if len(self.files) == 0:
            error("There were no files found to train from searching for `{}`".format(args.train),
                  "  - Try putting all your images in one folder and using `--train=data/*.jpg`")

        self.available = set(range(args.buffer_size))
        self.ready = set()

        self.cwd = os.getcwd()
        self.start()

    def run(self):
        while True:
            random.shuffle(self.files)

            for f in self.files:
                filename = os.path.join(self.cwd, f)
                try:
                    img = scipy.ndimage.imread(filename, mode='RGB')
                except Exception as e:
                    warn('Could not load `{}` as image.'.format(filename),
                         '  - Try fixing or removing the file before next run.')
                    files.remove(f)
                    continue
                
                for _ in range(args.buffer_similar):
                    copy = img[:,::-1] if random.choice([True, False]) else img
                    h = random.randint(0, copy.shape[0] - self.resolution)
                    w = random.randint(0, copy.shape[1] - self.resolution)
                    copy = copy[h:h+self.resolution, w:w+self.resolution]

                    while len(self.available) == 0:
                        self.data_copied.wait()
                        self.data_copied.clear()

                    i = self.available.pop()
                    self.buffer[i] = np.transpose(copy / 255.0 - 0.5, (2, 0, 1))
                    self.ready.add(i)

                    if len(self.ready) >= args.batch_size:
                        self.data_ready.set()

    def copy(self, output):
        self.data_ready.wait()
        self.data_ready.clear()

        for i, j in enumerate(random.sample(self.ready, args.batch_size)):
            output[i] = self.buffer[j]
            self.available.add(j)

        self.data_copied.set()


#======================================================================================================================
# Convolution Networks
#======================================================================================================================

class SubpixelReshuffleLayer(lasagne.layers.Layer):
    """Based on the code by ajbrock: https://github.com/ajbrock/Neural-Photo-Editor/
    """

    def __init__(self, incoming, channels, upscale, **kwargs):
        super(SubpixelReshuffleLayer, self).__init__(incoming, **kwargs)
        self.upscale = upscale
        self.channels = channels

    def get_output_shape_for(self, input_shape):
        def up(d): return self.upscale * d if d else d
        return (input_shape[0], self.channels, up(input_shape[2]), up(input_shape[3]))

    def get_output_for(self, input, deterministic=False, **kwargs):
        out, r = T.zeros(self.get_output_shape_for(input.shape)), self.upscale
        for y, x in itertools.product(range(r), repeat=2):
            out=T.inc_subtensor(out[:,:,y::r,x::r], input[:,r*y+x::r*r,:,:])
        return out


class Model(object):

    def __init__(self):
        self.network = collections.OrderedDict()
        if args.train:
            self.network['img'] = InputLayer((None, 3, None, None))
            self.network['seed'] = PoolLayer(self.network['img'], pool_size=2**args.scales, mode='average_exc_pad')
        else:
            self.network['img'] = InputLayer((None, 3, None, None))
            self.network['seed'] = self.network['img']

        config, params = self.load_model()
        self.setup_generator(self.last_layer(), config)

        if args.train:
            concatenated = lasagne.layers.ConcatLayer([self.network['img'], self.network['out']], axis=0)
            self.setup_perceptual(concatenated)
            self.load_perceptual()
            self.setup_discriminator()
        self.load_generator(params)

        self.compile()

    #------------------------------------------------------------------------------------------------------------------
    # Network Configuration
    #------------------------------------------------------------------------------------------------------------------

    def last_layer(self):
        return list(self.network.values())[-1]

    def make_layer(self, name, input, units, filter_size=(3,3), stride=(1,1), pad=(1,1), alpha=0.25):
        conv = ConvLayer(input, units, filter_size=filter_size, stride=stride, pad=pad, nonlinearity=None)
        prelu = lasagne.layers.ParametricRectifierLayer(conv, alpha=lasagne.init.Constant(alpha))
        self.network[name+'x'] = conv
        self.network[name+'>'] = prelu
        return prelu

    def make_block(self, name, input, units):
        self.make_layer(name+'-A', input, units, alpha=0.25)
        self.make_layer(name+'-B', self.last_layer(), units, alpha=1.0)
        return ElemwiseSumLayer([input, self.last_layer()]) if args.generator_residual else self.last_layer()

    def setup_generator(self, input, config):
        for k, v in config.items(): setattr(args, k, v)
        units_iter = extend(args.generator_filters)
        units = next(units_iter)
        self.make_layer('iter.0-A', input, units, filter_size=(5,5), pad=(2,2))
        self.make_layer('iter.0-B', self.last_layer(), units, filter_size=(5,5), pad=(2,2))
        self.network['iter.0'] = self.last_layer()

        for i in range(0, args.generator_blocks):
            self.make_block('iter.%i'%(i+1), self.last_layer(), units)

        for i in range(0, args.scales):
            u = next(units_iter)
            self.make_layer('scale%i.3'%i, self.last_layer(), u*4)
            self.network['scale%i.2'%i] = SubpixelReshuffleLayer(self.last_layer(), u, 2)
            self.make_layer('scale%i.1'%i, self.last_layer(), u)

        self.network['out'] = ConvLayer(self.last_layer(), 3, filter_size=(5,5), stride=(1,1), pad=(2,2),
                                                              nonlinearity=lasagne.nonlinearities.tanh)

    def setup_perceptual(self, input):
        """Use lasagne to create a network of convolution layers using pre-trained VGG19 weights.
        """

        offset = np.array([103.939, 116.779, 123.680], dtype=np.float32).reshape((1,3,1,1))
        self.network['percept'] = lasagne.layers.NonlinearityLayer(input, lambda x: ((x+0.5).clip(0.0, 1.0)*255.0) - offset)

        self.network['mse'] = self.network['percept']
        self.network['conv1_1'] = ConvLayer(self.network['percept'], 64, 3, pad=1)
        self.network['conv1_2'] = ConvLayer(self.network['conv1_1'], 64, 3, pad=1)
        self.network['pool1']   = PoolLayer(self.network['conv1_2'], 2, mode='max')
        self.network['conv2_1'] = ConvLayer(self.network['pool1'],   128, 3, pad=1)
        self.network['conv2_2'] = ConvLayer(self.network['conv2_1'], 128, 3, pad=1)
        self.network['pool2']   = PoolLayer(self.network['conv2_2'], 2, mode='max')
        self.network['conv3_1'] = ConvLayer(self.network['pool2'],   256, 3, pad=1)
        self.network['conv3_2'] = ConvLayer(self.network['conv3_1'], 256, 3, pad=1)
        self.network['conv3_3'] = ConvLayer(self.network['conv3_2'], 256, 3, pad=1)
        self.network['conv3_4'] = ConvLayer(self.network['conv3_3'], 256, 3, pad=1)
        self.network['pool3']   = PoolLayer(self.network['conv3_4'], 2, mode='max')
        self.network['conv4_1'] = ConvLayer(self.network['pool3'],   512, 3, pad=1)
        self.network['conv4_2'] = ConvLayer(self.network['conv4_1'], 512, 3, pad=1)
        self.network['conv4_3'] = ConvLayer(self.network['conv4_2'], 512, 3, pad=1)
        self.network['conv4_4'] = ConvLayer(self.network['conv4_3'], 512, 3, pad=1)
        self.network['pool4']   = PoolLayer(self.network['conv4_4'], 2, mode='max')
        self.network['conv5_1'] = ConvLayer(self.network['pool4'],   512, 3, pad=1)
        self.network['conv5_2'] = ConvLayer(self.network['conv5_1'], 512, 3, pad=1)
        self.network['conv5_3'] = ConvLayer(self.network['conv5_2'], 512, 3, pad=1)
        self.network['conv5_4'] = ConvLayer(self.network['conv5_3'], 512, 3, pad=1)

    def setup_discriminator(self):
        c = args.discriminator_size
        self.make_layer('disc1.1', batch_norm(self.network['conv1_2']), 1*c, filter_size=(5,5), stride=(2,2), pad=(2,2))
        self.make_layer('disc1.2', self.last_layer(), 1*c, filter_size=(5,5), stride=(2,2), pad=(2,2))
        self.make_layer('disc2', batch_norm(self.network['conv2_2']), 2*c, filter_size=(5,5), stride=(2,2), pad=(2,2))
        self.make_layer('disc3', batch_norm(self.network['conv3_2']), 3*c, filter_size=(3,3), stride=(1,1), pad=(1,1))
        hypercolumn = ConcatLayer([self.network['disc1.2>'], self.network['disc2>'], self.network['disc3>']])
        self.make_layer('disc4', hypercolumn, 4*c, filter_size=(1,1), stride=(1,1), pad=(0,0))
        self.make_layer('disc5', self.last_layer(), 3*c, filter_size=(3,3), stride=(2,2))
        self.make_layer('disc6', self.last_layer(), 2*c, filter_size=(1,1), stride=(1,1), pad=(0,0))
        self.network['disc'] = batch_norm(ConvLayer(self.last_layer(), 1, filter_size=(1,1),
                                                    nonlinearity=lasagne.nonlinearities.linear))


    #------------------------------------------------------------------------------------------------------------------
    # Input / Output
    #------------------------------------------------------------------------------------------------------------------

    def load_perceptual(self):
        """Open the serialized parameters from a pre-trained network, and load them into the model created.
        """
        vgg19_file = os.path.join(os.path.dirname(__file__), 'vgg19_conv.pkl.bz2')
        if not os.path.exists(vgg19_file):
            error("Model file with pre-trained convolution layers not found. Download here...",
                  "https://github.com/alexjc/neural-doodle/releases/download/v0.0/vgg19_conv.pkl.bz2")

        data = pickle.load(bz2.open(vgg19_file, 'rb'))
        layers = lasagne.layers.get_all_layers(self.last_layer(), treat_as_input=[self.network['percept']])
        for p, d in zip(itertools.chain(*[l.get_params() for l in layers]), data): p.set_value(d)

    def list_generator_layers(self):
        for l in lasagne.layers.get_all_layers(self.network['out'], treat_as_input=[self.network['img']]):
            if not l.get_params(): continue
            name = list(self.network.keys())[list(self.network.values()).index(l)]
            yield (name, l)

    def save_generator(self):
        def cast(p): return p.get_value().astype(np.float16)
        params = {k: [cast(p) for p in l.get_params()] for (k, l) in self.list_generator_layers()}
        config = {k: getattr(args, k) for k in ['generator_blocks', 'generator_residual', 'generator_filters']}
        filename = 'ne%ix-%s-%s.pkl.bz2' % (2**args.scales, args.model, __version__)
        pickle.dump((config, params), bz2.open(filename, 'wb'))
        print('  - Saved model as `{}` after training.'.format(filename))

    def load_model(self):
        filename = 'ne%ix-%s-%s.pkl.bz2' % (2**args.scales, args.model, __version__)
        if not os.path.exists(filename):
            if args.train: return {}, {}
            error("Model file with pre-trained convolution layers not found. Download it here...",
                  "https://github.com/alexjc/neural-enhance/releases/download/v%s/%s"%(__version__, filename))
        print('  - Loaded file `{}` with trained model.'.format(filename))
        return pickle.load(bz2.open(filename, 'rb'))

    def load_generator(self, params):
        if len(params) == 0: return
        for k, l in self.list_generator_layers():
            assert k in params, "Couldn't find layer `%s` in loaded model.'" % k
            assert len(l.get_params()) == len(params[k]), "Mismatch in types of layers."
            for p, v in zip(l.get_params(), params[k]):
                assert v.shape == p.get_value().shape, "Mismatch in number of parameters."
                p.set_value(v.astype(np.float32))

    #------------------------------------------------------------------------------------------------------------------
    # Training & Loss Functions
    #------------------------------------------------------------------------------------------------------------------

    def loss_perceptual(self, p):
        return lasagne.objectives.squared_error(p[:args.batch_size], p[args.batch_size:]).mean()

    def loss_total_variation(self, x):
        return T.mean(((x[:,:,:-1,:-1] - x[:,:,1:,:-1])**2 + (x[:,:,:-1,:-1] - x[:,:,:-1,1:])**2)**1.25)

    def loss_adversarial(self, d):
        return T.mean(1.0 - T.nnet.softplus(d[args.batch_size:]))

    def loss_discriminator(self, d):
        return T.mean(T.nnet.softminus(d[args.batch_size:]) - T.nnet.softplus(d[:args.batch_size]))

    def compile(self):
        # Helper function for rendering test images during training, or standalone non-training mode.
        input_tensor = T.tensor4()
        input_layers = {self.network['img']: input_tensor}
        output = lasagne.layers.get_output([self.network[k] for k in ['img', 'seed', 'out']], input_layers, deterministic=True)
        self.predict = theano.function([input_tensor], output)

        if not args.train: return

        output_layers = [self.network['out'], self.network[args.perceptual_layer], self.network['disc']]
        gen_out, percept_out, disc_out = lasagne.layers.get_output(output_layers, input_layers, deterministic=False)

        # Generator loss function, parameters and updates.
        self.gen_lr = theano.shared(np.array(0.0, dtype=theano.config.floatX))
        self.adversary_weight = theano.shared(np.array(0.0, dtype=theano.config.floatX))
        gen_losses = [self.loss_perceptual(percept_out) * args.perceptual_weight,
                      self.loss_total_variation(gen_out) * args.smoothness_weight,
                      self.loss_adversarial(disc_out) * self.adversary_weight]
        gen_params = lasagne.layers.get_all_params(self.network['out'], trainable=True)
        print('  - {} tensors learned for generator.'.format(len(gen_params)))
        gen_updates = lasagne.updates.adam(sum(gen_losses, 0.0), gen_params, learning_rate=self.gen_lr)

        # Discriminator loss function, parameters and updates.
        self.disc_lr = theano.shared(np.array(0.0, dtype=theano.config.floatX))
        disc_losses = [self.loss_discriminator(disc_out)]
        disc_params = list(itertools.chain(*[l.get_params() for k, l in self.network.items() if 'disc' in k]))
        print('  - {} tensors learned for discriminator.'.format(len(disc_params)))
        disc_updates = lasagne.updates.adam(sum(disc_losses, 0.0), disc_params, learning_rate=self.disc_lr)

        # Combined Theano function for updating both generator and discriminator at the same time.
        updates = collections.OrderedDict(list(gen_updates.items()) + list(disc_updates.items()))
        self.fit = theano.function([input_tensor], gen_losses + [disc_out.mean(axis=(1,2,3))], updates=updates)



class NeuralEnhancer(object):

    def __init__(self):
        if args.train:
            print('{}Training {} epochs on random image sections with batch size {}.{}'\
                  .format(ansi.BLUE_B, args.epochs, args.batch_size, ansi.BLUE))
        else:
            if len(args.files) == 0: error("Specify the image(s) to enhance on the command-line.")
            print('{}Enhancing {} image(s) specified on the command-line.{}'\
                  .format(ansi.BLUE_B, len(args.files), ansi.BLUE))

        self.thread = DataLoader() if args.train else None
        self.model = Model()

        print('{}'.format(ansi.ENDC))

    def imsave(self, fn, img):
        img = np.transpose(img + 0.5, (1, 2, 0)).clip(0.0, 1.0)
        image = scipy.misc.toimage(img * 255.0, cmin=0, cmax=255)
        image.save(fn)

    def show_progress(self, orign, scald, repro):
        os.makedirs('valid', exist_ok=True)
        for i in range(args.batch_size):
            self.imsave('valid/%03i_origin.png' % i, orign[i])
            self.imsave('valid/%03i_pixels.png' % i, scald[i])
            self.imsave('valid/%03i_reprod.png' % i, repro[i])

    def decay_learning_rate(self):
        l_r, t_cur = args.learning_rate, 0

        while True:
            yield l_r if t_cur > 0 else l_r * 0.1
            t_cur += 1
            if t_cur % args.learning_period == 0: l_r *= args.learning_decay

    def train(self):
        images = np.zeros((args.batch_size, 3, args.batch_resolution, args.batch_resolution), dtype=np.float32)
        learning_rate = self.decay_learning_rate()
        try:
            running, start = None, time.time()
            for epoch in range(args.epochs):
                total, stats = None, None
                l_r = next(learning_rate)
                if epoch >= args.generator_start: self.model.gen_lr.set_value(l_r)
                if epoch >= args.discriminator_start: self.model.disc_lr.set_value(l_r)

                for _ in range(args.epoch_size):
                    self.thread.copy(images)
                    output = self.model.fit(images)
                    losses = np.array(output[:3], dtype=np.float32)
                    stats = (stats + output[3]) if stats is not None else output[3]
                    total = total + losses if total is not None else losses
                    l = np.sum(losses)
                    assert not np.isnan(losses).any()
                    running = l if running is None else running * 0.95 + 0.05 * l
                    print('↑' if l > running else '↓', end='', flush=True)

                orign, scald, repro = self.model.predict(images)
                self.show_progress(orign, scald, repro)
                total /= args.epoch_size
                stats /= args.epoch_size
                totals, labels = [sum(total)] + list(total), ['total', 'prcpt', 'smthn', 'advrs']
                gen_info = ['{}{}{}={:4.2e}'.format(ansi.WHITE_B, k, ansi.ENDC, v) for k, v in zip(labels, totals)]
                print('\rEpoch #{} at {:4.1f}s, lr={:4.2e}    {}'.format(epoch+1, time.time()-start, l_r, ' '*args.epoch_size))
                print('  - generator {}'.format(' '.join(gen_info)))

                real, fake = stats[:args.batch_size], stats[args.batch_size:]
                print('  - discriminator', real.mean(), len(np.where(real > 0.5)[0]), fake.mean(), len(np.where(fake < -0.5)[0]))
                if epoch == args.adversarial_start-1:
                    print('  - adversary mode: generator engaging discriminator.')
                    self.model.adversary_weight.set_value(args.adversary_weight)
                    running = None

        except KeyboardInterrupt:
            pass

        print('\n{}Trained {}x super-resolution for {} epochs.{}'\
                .format(ansi.CYAN_B, 2**args.scales, epoch+1, ansi.CYAN))
        self.model.save_generator()
        print(ansi.ENDC)

    def process(self, image):
        img = np.transpose(image / 255.0 - 0.5, (2, 0, 1))[np.newaxis].astype(np.float32)
        *_, repro = self.model.predict(img)
        repro = np.transpose(repro[0] + 0.5, (1, 2, 0)).clip(0.0, 1.0)
        return scipy.misc.toimage(repro * 255.0, cmin=0, cmax=255)


if __name__ == "__main__":
    enhancer = NeuralEnhancer()

    if args.train:
        enhancer.train()

    for filename in args.files:
        print(filename)
        out = enhancer.process(scipy.ndimage.imread(filename, mode='RGB'))
        out.save(os.path.splitext(filename)[0]+'_ne%ix.png'%(2**args.scales))

    if args.files:
        print(ansi.ENDC)
