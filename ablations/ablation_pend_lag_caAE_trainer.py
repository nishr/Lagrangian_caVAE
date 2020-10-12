# Standard library imports
from argparse import ArgumentParser
import os, sys
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PARENT_DIR)

# Third party imports
import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader
import pytorch_lightning as pl
from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning.callbacks import ModelCheckpoint
from torchdiffeq import odeint

# local application imports
from lag_caVAE.lag import Lag_Net
from lag_caVAE.nn_models import MLP_Encoder, MLP, MLP_Decoder, PSD
from hyperspherical_vae.distributions import VonMisesFisher
from hyperspherical_vae.distributions import HypersphericalUniform
from utils import arrange_data, from_pickle, my_collate, ImageDataset

seed_everything(0)


class Model(pl.LightningModule):

    def __init__(self, hparams, data_path=None):
        super(Model, self).__init__()
        self.hparams = hparams
        self.data_path = data_path
        self.T_pred = self.hparams.T_pred
        self.loss_fn = torch.nn.MSELoss(reduction='none')

        self.recog_q_net = MLP_Encoder(32*32, 300, 2, nonlinearity='elu')
        self.obs_net = MLP_Encoder(1, 100, 32*32, nonlinearity='elu')
        V_net = MLP(2, 50, 1) ; g_net = MLP(2, 50, 1) ; M_net = PSD(2, 50, 1)
        self.ode = Lag_Net(q_dim=1, u_dim=1, g_net=g_net, M_net=M_net, V_net=V_net)

    def train_dataloader(self):
        train_dataset = ImageDataset(self.data_path, self.hparams.T_pred)
        self.t_eval = torch.from_numpy(train_dataset.t_eval)
        return DataLoader(train_dataset, batch_size=self.hparams.batch_size, shuffle=True, collate_fn=my_collate)

    def angle_vel_est(self, q0_m_n, q1_m_n, delta_t):
        delta_cos = q1_m_n[:,0:1] - q0_m_n[:,0:1]
        delta_sin = q1_m_n[:,1:2] - q0_m_n[:,1:2]
        q_dot0 = - delta_cos * q0_m_n[:,1:2] / delta_t + delta_sin * q0_m_n[:,0:1] / delta_t
        return q_dot0

    def encode(self, batch_image):
        q = self.recog_q_net(batch_image)
        q_n = q / q.norm(dim=-1, keepdim=True)
        return q, q_n

    def get_theta_inv(self, cos, sin, x, y, bs=None):
        bs = self.bs if bs is None else bs
        theta = torch.zeros([bs, 2, 3], dtype=self.dtype, device=self.device)
        theta[:, 0, 0] += cos ; theta[:, 0, 1] += -sin ; theta[:, 0, 2] += - x * cos + y * sin
        theta[:, 1, 0] += sin ; theta[:, 1, 1] += cos ;  theta[:, 1, 2] += - x * sin - y * cos
        return theta

    def forward(self, X, u):
        [_, self.bs, d, d] = X.shape
        T = len(self.t_eval)
        # encode
        self.q0, self.q0_n = self.encode(X[0].reshape(self.bs, d*d))
        self.q1, self.q1_n = self.encode(X[1].reshape(self.bs, d*d))

        # estimate velocity
        self.q_dot0 = self.angle_vel_est(self.q0_n, self.q1_n, self.t_eval[1]-self.t_eval[0])

        # predict
        z0_u = torch.cat((self.q0_n, self.q_dot0, u), dim=1)
        zT_u = odeint(self.ode, z0_u, self.t_eval, method=self.hparams.solver) # T, bs, 4
        self.qT, self.q_dotT, _ = zT_u.split([2, 1, 1], dim=-1)
        self.qT = self.qT.view(T*self.bs, 2)

        # decode
        ones = torch.ones_like(self.qT[:, 0:1])
        self.content = self.obs_net(ones)

        theta = self.get_theta_inv(self.qT[:, 0], self.qT[:, 1], 0, 0, bs=T*self.bs) # cos , sin 

        grid = F.affine_grid(theta, torch.Size((T*self.bs, 1, d, d)))
        self.Xrec = F.grid_sample(self.content.view(T*self.bs, 1, d, d), grid)
        self.Xrec = self.Xrec.view([T, self.bs, d, d])
        return None

    def training_step(self, train_batch, batch_idx):
        X, u = train_batch
        self.forward(X, u)

        lhood = - self.loss_fn(self.Xrec, X)
        lhood = lhood.sum([0, 2, 3]).mean()
        norm_penalty = (self.q0.norm(dim=-1).mean() - 1) ** 2

        loss = - lhood + 1/100 * norm_penalty

        logs = {'recon_loss': -lhood, 'train_loss': loss}
        return {'loss':loss, 'log': logs, 'progress_bar': logs}

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), self.hparams.learning_rate)

    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Specify the hyperparams for this LightningModule
        """
        # MODEL specific
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument('--learning_rate', default=1e-3, type=float)
        parser.add_argument('--batch_size', default=512, type=int)

        return parser


def main(args):
    model = Model(hparams=args, data_path=os.path.join(PARENT_DIR, 'datasets', 'pendulum-gym-image-dataset-train.pkl'))
    checkpoint_callback = ModelCheckpoint(monitor='loss', 
                                          prefix=args.name+f'-T_p={args.T_pred}-', 
                                          save_top_k=1, 
                                          save_last=True)
    trainer = Trainer.from_argparse_args(args, 
                                         deterministic=True,
                                         default_root_dir=os.path.join(PARENT_DIR, 'logs', args.name),
                                         checkpoint_callback=checkpoint_callback) 
    trainer.fit(model)


if __name__ == '__main__':
    parser = ArgumentParser(add_help=False)
    parser.add_argument('--name', default='ablation-pend-lag-caAE', type=str)
    parser.add_argument('--T_pred', default=4, type=int)
    parser.add_argument('--solver', default='euler', type=str)
    # add args from trainer
    parser = Trainer.add_argparse_args(parser)
    # give the module a chance to add own params
    # good practice to define LightningModule speficic params in the module
    parser = Model.add_model_specific_args(parser)
    # parse params
    args = parser.parse_args()

    main(args)