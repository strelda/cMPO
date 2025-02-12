""" utility functions for cMPO method
"""

import numpy as np
import torch 
import os, io, subprocess
import os.path
torch.set_num_threads(int(os.environ['OMP_NUM_THREADS']))

def eigensolver(M):
    """ Eigensolver
        manually symmetrize M before the eigen decomposition
    """
    return torch.symeig(0.5*(M+M.t()), eigenvectors=True)

class LogTrExpm(torch.autograd.Function):
    @staticmethod
    def forward(self, beta, mat):
        dtype, device = mat.dtype, mat.device
        #rho = torch.matrix_exp(beta*mat)
        #tr_rho = torch.trace(rho)
        w, v = eigensolver(mat)
        y = torch.logsumexp(beta*w, dim=0)
        scaled_rho = beta * v @ torch.diag(torch.exp(beta*w-y)) @ v.t()
        self.save_for_backward(scaled_rho)
        return y

    @staticmethod
    def backward(self, dy):
        scaled_rho = self.saved_tensors[0]
        dmat = dy * scaled_rho.t()
        return None, dmat

class cmpo(object):
    """ the object for cMPO
        dim: the physical dimension of the cMPO
        the structure of cMPO 
            --                              --
            | I + dtau Q  -- sqrt(dtau) R -- |
            |                                |
            |       |                        |
            | sqrt(dtau) L        P          |
            |       |                        |
            --                              --
    """
    def __init__(self, Q, L, R, P):
        self.dim = Q.shape[0]
        self.dtype = Q.dtype
        self.device = Q.device
        self.Q = Q # 2 leg: D x D
        self.L = L # 3 leg: d x D x D
        self.R = R # 3 leg: d x D x D
        self.P = P # 4 leg: d x d x D x D

    def detach(self):
        """ return the detached cMPO object, clear autograd information
        """
        return cmpo(self.Q.detach(), self.L.detach(), self.R.detach(), self.P.detach())

    def project(self, U):
        """ perform a unitary transformation in the imaginary-time direction
            if U is a square matrix, this is a guage transformation
        """
        Q = U.t() @ self.Q @ U
        L = U.t() @ self.L @ U
        R = U.t() @ self.R @ U
        P = U.t() @ self.P @ U
        return cmpo(Q, L, R, P)

    def t(self):
        """ give the transpose of the cMPO
        """
        Q = self.Q
        L = self.R
        R = self.L
        P = torch.einsum('abmn->bamn', self.P)
        return cmpo(Q, L, R, P)

class cmps(object):
    """ the object for cMPS
        dim: the physical dimension of the cMPS
        the structure of cMPS 
            --            --
            | I + dtau Q   |
            |              |
            |       |      |
            | sqrt(dtau) R |
            |       |      |
            --            --
    """
    def __init__(self, Q, R):
        self.dim = Q.shape[0]
        self.dtype = Q.dtype
        self.device = Q.device
        self.Q = Q
        self.R = R

    def detach(self):
        """ return the detached cMPS object, clear autograd information
        """
        return cmps(self.Q.detach(), self.R.detach())

    def project(self, U):
        """ perform a unitary transformation in the imaginary-time direction
            if U is a square matrix, this is a guage transformation
        """
        Q = U.t() @ self.Q @ U
        R = U.t() @ self.R @ U
        return cmps(Q, R)

    def diagQ(self):
        """ transform the cMPS to the gauge where Q is a diagonalized matrix 
        """
        _, U = eigensolver(self.Q)
        return self.project(U)

def multiply(W, mps):
    """ multiply a matrix to the left of the cMPS
             --        --   --            --
             | 1 0 ... 0|   | I + dtau Q   |
             | 0        |   |              |
             | :        |   |       |      |
             | :    W   |   | sqrt(dtau) R |
             | 0        |   |       |      |
             --        --   --            --
    """
    dtype, device = mps.dtype, mps.device
    R1 = torch.einsum('mn, nab->mab', W, mps.R)
    return cmps(mps.Q, R1)

def act(mpo, mps):
    """ act the cmps to the right of cmpo
             --                              --   --            --
             | I + dtau Q  -- sqrt(dtau) R -- |   | I + dtau Q   |
             |                                |   |              |
             |       |                        |   |       |      |
             | sqrt(dtau) L        P          |   | sqrt(dtau) R |
             |       |                        |   |       |      |
             --                              --   --            --
    """
    dtype, device = mps.dtype, mps.device
    Do, Ds = mpo.dim, mps.dim
    d = mps.R.shape[0]
    Io = torch.eye(Do, dtype=dtype, device=device) 
    Is = torch.eye(Ds, dtype=dtype, device=device)

    Q_rslt = torch.einsum('ab,cd->acbd', mpo.Q, Is).contiguous().view(Do*Ds, Do*Ds) \
           + torch.einsum('ab,cd->acbd', Io, mps.Q).contiguous().view(Do*Ds, Do*Ds) \
           + torch.einsum('mab,mcd->acbd', mpo.R, mps.R).contiguous().view(Do*Ds, Do*Ds) 
    R_rslt = torch.einsum('mab,mcd->macbd', mpo.L, Is.repeat(d,1,1)).contiguous().view(d, Do*Ds, Do*Ds) \
           + torch.einsum('mnab,ncd->macbd', mpo.P, mps.R).contiguous().view(d, Do*Ds, Do*Ds)

    return cmps(Q_rslt, R_rslt)

def Lact(mps, mpo):
    """ act the cmps to the left of cmpo
          --            --  --                              --   
          | I + dtau Q   |  | I + dtau Q  -- sqrt(dtau) R -- |   
          |              |  |                                |   
          |       |      |  |       |                        |   
          | sqrt(dtau) R |  | sqrt(dtau) L        P          |   
          |       |      |  |       |                        |   
          --            --  --                              --   
    """
    dtype, device = mps.dtype, mps.device
    Do, Ds = mpo.dim, mps.dim
    d = mps.R.shape[0]

    Tmps = act(mpo.t(), mps)
    Q = torch.einsum('abcd->badc', Tmps.Q.view(Do, Ds, Do, Ds)).contiguous().view(Do*Ds, Do*Ds)
    R = torch.einsum('mabcd->mbadc', Tmps.R.view(d, Do, Ds, Do, Ds)).contiguous().view(d, Do*Ds, Do*Ds)
    return cmps(Q, R)

def density_matrix(mps1, mps2):
    """ construct the K matrix corresponding to <mps1|mps2>
       --                          --  --             --     
       |                            |  | I + dtau Q2   |     
       |                            |  |       |       |     
       | I + dtau Q1 sqrt(dtau) R1  |  | sqrt(dtau) R2 |   = I + dtau K 
       |                            |  |       |       |     
       --                          --  --             --     
    """
    dtype, device= mps1.dtype, mps1.device
    D1, D2 = mps1.dim, mps2.dim
    I1 = torch.eye(mps1.dim, dtype=dtype, device=device)
    I2 = torch.eye(mps2.dim, dtype=dtype, device=device) 

    M = torch.einsum('ab,cd->acbd', mps1.Q, I2).contiguous().view(D1*D2, D1*D2) \
      + torch.einsum('ab,cd->acbd', I1, mps2.Q).contiguous().view(D1*D2, D1*D2) \
      + torch.einsum('mab,mcd->acbd', mps1.R, mps2.R).contiguous().view(D1*D2, D1*D2) 
    return M

def ln_ovlp(mps1, mps2, beta):
    """ calculate log(<mps1|mps2>)
    """
    M = density_matrix(mps1, mps2)
    return LogTrExpm.apply(beta, M)

def Fidelity(psi, mps, beta):
    """ calculate log [ <psi|mps> / sqrt(<psi|psi>) ]
    """
    up = ln_ovlp(psi, mps, beta)
    dn = ln_ovlp(psi, psi, beta)
    return up - 0.5*dn

def energy_cut(mps, chi):
    """initialize the isometry 
    keep the chi largest eigenvalues in the Q matrix of the cMPS
    """
    w, v = eigensolver(mps.Q)
    P = v[:, -chi:]
    return P

def interpolate_cut(cut1, cut2, theta):
    """ interpolate two isometries
     theta = pi/2: mix = cut1
     theta = 0   : mix = cut2
    """
    mix = np.sin(theta) * cut1 + np.cos(theta) * cut2
    U, _, V = torch.svd(mix)
    return U@V.t()

def adaptive_mera_update(mps, beta, chi, tol=1e-12, maxiter=50):
    """ update the isometry using iterative SVD update with line search
        mps: the original cMPS
        beta: inverse temperature
        chi: target bond dimension
        return the compressed cMPS
    """
    P = energy_cut(mps, chi)
    last = 9.9e9
    step = 0
    while step < maxiter:
        mps_new = mps.project(P.requires_grad_())
        loss = ln_ovlp(mps_new, mps, beta) - 0.5 * ln_ovlp(mps_new, mps_new.detach(), beta) 
        diff = abs(loss.item() - last)

        #print('adaptive', step, loss.item() - 0.5*ln_ovlp(mps, mps, beta).item())

        if (diff < tol): break

        print(step, end='\r')

        grad = torch.autograd.grad(loss, P)[0]
        last = loss.item() 
        Fidel0 = loss.item() 
        Fidel_test = 1e99

        step += 1
    
        U, _, V = torch.svd(grad)
        #https://mathoverflow.net/questions/262560/natural-ways-of-interpolating-unitary-matrices
        #https://groups.google.com/forum/#!topic/manopttoolbox/2zhx67doXaU
        #interpolate between unitary matrices
        theta = np.pi
        proceed = False
        while proceed == False:
            theta = theta / 2
            if theta < np.pi / 1.9**12: 
                theta = 0
                P_test = P.data
            else:
                P_test = interpolate_cut(U@V.t(), P.data, theta)

            #mix = np.sin(theta) * U@V.t() + np.cos(theta) * P.data
            ##then retraction back to unitary
            #U, _, V = torch.svd(mix)
            #P_test = U@V.t()

            mps_test = mps.project(P_test)
            Fidel1_test = Fidelity(mps_test, mps, beta)
            if Fidel1_test > Fidel0 or np.isclose(theta, 0):
                P = P_test
                proceed=True

    return mps_new 

def variational_compr(mps, beta, chi, chkp_loc, init=None, tol=1e-12):
    """ variationally optimize the compressed cMPS 
        mps: the original cMPS
        beta: the inverse temperature
        chi: target bond dimension
        chkp_loc: the location to save check point datafile
        tol: tolerance
        return the compressed cMPS
    """
    if init is None: 
        psi = adaptive_mera_update(mps, beta, chi, tol=tol)
        psi = psi.diagQ()
    else:
        psi = init
    Q = torch.nn.Parameter(torch.diag(psi.Q))
    R = torch.nn.Parameter(psi.R)
    psi_data = data_cmps(Q, R)

    optimizer = torch.optim.LBFGS([Q, R], max_iter=20, tolerance_grad=0, tolerance_change=0, line_search_fn="strong_wolfe") 

    def closure():
        optimizer.zero_grad()
        psi = cmps(torch.diag(Q), R)
        loss = - Fidelity(psi, mps, beta)
        loss.backward()
        return loss

    is_converged = False
    loss0 = 9.99e99
    while not is_converged:
        loss = optimizer.step(closure)
        print('--> ' + '{:.12f}'.format(loss.item()), end='\r')
        is_converged = np.isclose(loss.item(), loss0, rtol=tol, atol=tol)
        loss0 = loss.item()

    # "normalize"
    with torch.no_grad():
        Q -= torch.max(Q)
        psi = cmps(torch.diag(Q), R) 
    # checkpoint
    datasave(psi_data, chkp_loc)

    return psi.detach()

# utility functions for save and load datafile
class data_cmps(torch.nn.Module):
    def __init__(self, Q, R):
        super(data_cmps, self).__init__()
        self.Q = Q 
        self.R = R
def datasave(model, path):
    torch.save(model.state_dict(), path)
def dataload(model, path):
    model.load_state_dict(torch.load(path))
    model.eval()

