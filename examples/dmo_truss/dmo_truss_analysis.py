# Import numpy 
import numpy as np
import scipy.linalg as linalg

# Import parts of matplotlib for plotting
import matplotlib.pyplot as plt

# Import MPI
from mpi4py import MPI

# Import ParOpt
from paropt import ParOpt

class TrussAnalysis(ParOpt.pyParOptProblem):
    def __init__(self, conn, xpos, loads, bcs, 
                 E, rho, Avals, m_fixed, use_mass_constraint=True,
                 x_lb=0.0, epsilon=1e-3, sigma=10.0, no_bound=1e30):
        '''
        Analysis problem for mass-constrained compliance minimization
        '''

        # Store pointer to the data
        self.conn = conn
        self.xpos = xpos
        self.loads = loads
        self.bcs = bcs

        # Set the value of the Young's modulus
        self.E = E
        
        # Set the values of the areas -- all must be non-zero
        self.Avals = Avals
        self.rho = rho

        # Fixed mass value
        self.m_fixed = m_fixed

        # Set the factor on the lowest value of the thickness
        # This avoids
        self.epsilon = epsilon

        # Set the bound for no variable bound
        self.no_bound = no_bound

        # Keep a vector that stores the element areas
        self.A = np.zeros(len(self.conn))

        # Set the value of sigma
        self.sigma = sigma

        # Set the sizes for the problem
        self.nmats = len(self.Avals)
        self.nblock = self.nmats+1
        self.nelems = len(self.conn)
        self.nvars = len(self.xpos)

        # Set the flag for whether to use a mass constraint or not
        self.use_mass_constraint = use_mass_constraint

        # Initialize the super class
        ncon = 0
        if self.use_mass_constraint:
            ncon = 1
        nwcon = self.nelems
        nwblock = 1
        ndv = self.nblock*self.nelems
        super(TrussAnalysis, self).__init__(MPI.COMM_SELF,
                                            ndv, ncon, nwcon, nwblock)

        # Set the penalization
        self.penalty = np.zeros(ndv)
        self.xinit = np.zeros(ndv)

        # Set the initial variable values
        self.xinit[:] = 1.0/self.nmats
        self.xinit[::self.nblock] = 0.5

        # Set the lower bounds on the variables
        self.x_lb = max(x_lb, 0.0)

        # Allocate the matrices required
        self.K = np.zeros((self.nvars, self.nvars))
        self.Kp = np.zeros((self.nvars, self.nvars))
        self.f = np.zeros(self.nvars)
        self.u = np.zeros(self.nvars)
        self.phi = np.zeros(self.nvars)
        
        # Set the scaling of the objective
        self.obj_scale = None

        # Keep track of the different counts
        self.fevals = 0
        self.gevals = 0
        self.hevals = 0

        # Allocate a vector that stores the gradient of the mass
        self.gmass = np.zeros(ndv)

        # Compute the gradient of the mass for each bar in the mesh
        index = 0
        for bar in self.conn:
            # Get the first and second node numbers from the bar
            n1 = bar[0]
            n2 = bar[1]

            # Compute the nodal locations
            xd = self.xpos[2*n2] - self.xpos[2*n1]
            yd = self.xpos[2*n2+1] - self.xpos[2*n1+1]
            Le = np.sqrt(xd**2 + yd**2)

            for j in xrange(self.nmats):
                self.gmass[self.nblock*index+1+j] += self.rho[j]*Le

            index += 1
            
        return

    def setNewInitPointPenalty(self, x, gamma):
        '''
        Set the linearized penalty function, given the design variable
        values from the previous iteration and the penalty parameters
        '''

        # Set the new initial design variable values
        self.xinit[:] = x[:]

        # Modify the variables to lie within the prescribed bounds
        bound = 2e-3
        for i in xrange(self.nelems):
            # Modify the bounds of the thickness variables
            if self.xinit[i*self.nblock] > 1.0 - bound:
                self.xinit[i*self.nblock] = 1.0 - bound

            # Check the bounds of the material selection variables
            for j in xrange(1, self.nblock):
                if self.xinit[i*self.nblock+j] - self.x_lb < bound:
                    self.xinit[i*self.nblock+j] = self.x_lb + bound

        # Set the penalty parameters
        for i in xrange(self.nelems):
            self.penalty[self.nblock*i] = gamma[i]*(1.0 - x[self.nblock*i])
            for j in xrange(1, self.nblock):
                self.penalty[self.nblock*i+j] = -gamma[i]*x[self.nblock*i+j]

        return

    def getDiscreteInfeas(self, x):
        '''
        Compute the discrete infeasibility measure at a given design point
        '''
        
        d = np.zeros(self.nelems)
        for i in xrange(self.nelems):
            tnum = self.nblock*i
            d[i] = 1.0 - (x[tnum] - 1.0)**2 - sum(x[tnum+1:tnum+self.nblock]**2)
            
        return d

    def getStrainEnergy(self, x):
        '''Compute the strain energy in each bar'''

        Ue = np.zeros(self.nelems)

        # Set the cross-sectional areas from the design variable
        # values
        self.setAreas(x, lb_factor=self.epsilon)

        # Evaluate compliance objective
        self.assembleMat(self.A, self.K)
        self.assembleLoadVec(self.f)
        self.applyBCs(self.K, self.f)

        # Copy the values
        self.u[:] = self.f[:]

        # Perform the Cholesky factorization
        self.L = linalg.cholesky(self.K, lower=True)
            
        # Solve the resulting linear system of equations
        linalg.solve_triangular(self.L, self.u, lower=True,
                                trans='N', overwrite_b=True)
        linalg.solve_triangular(self.L, self.u, lower=True, 
                                trans='T', overwrite_b=True)

        # Add up the contribution to the gradient
        index = 0
        for bar, A_bar in zip(self.conn, self.A):
            # Get the first and second node numbers from the bar
            n1 = bar[0]
            n2 = bar[1]

            # Compute the nodal locations
            xd = self.xpos[2*n2] - self.xpos[2*n1]
            yd = self.xpos[2*n2+1] - self.xpos[2*n1+1]
            Le = np.sqrt(xd**2 + yd**2)
            C = xd/Le
            S = yd/Le

            # Compute the element stiffness matrix
            Ke = (self.E*A_bar/Le)*np.array(
                [[C**2, C*S, -C**2, -C*S],
                 [C*S, S**2, -C*S, -S**2],
                 [-C**2, -C*S, C**2, C*S],
                 [-C*S, -S**2, C*S, S**2]])
            
            # Create a list of the element variables for convenience
            elem_vars = [2*n1, 2*n1+1, 2*n2, 2*n2+1]
            
            # Add the product to the derivative of the compliance
            for i in xrange(4):
                for j in xrange(4):
                    Ue[index] += 0.5*self.u[elem_vars[i]]*self.u[elem_vars[j]]*Ke[i, j]

            index += 1

        return Ue

    def getVarsAndBounds(self, x, lb, ub):
        '''Get the variable values and bounds'''
        
        # Set the variable values
        x[:] = self.xinit[:]

        # Set the bounds on the material selection variables
        lb[:] = max(0.0, self.x_lb)
        ub[:] = self.no_bound

        # Set the bounds on the thickness variables
        lb[::self.nblock] = -self.no_bound
        ub[::self.nblock] = 1.0

        return

    def setAreas(self, x, lb_factor=0.0):
        '''Set the areas from the design variable values'''
        
        # Zero all the areas
        self.A[:] = lb_factor*self.Avals[0]

        # Add up the contributions to the areas from each 
        # discrete variable
        for i in xrange(len(self.conn)):
            for j in xrange(self.nmats):
                self.A[i] += self.Avals[j]*(lb_factor + x[i*self.nblock+1+j])

        return

    def getMass(self, x):
        '''Return the mass of the truss'''
        return np.dot(self.gmass, x)    

    def evalObjCon(self, x):
        '''
        Evaluate the objective (compliance) and constraint (mass)
        '''
        
        # Add the number of function evaluations
        self.fevals += 1

        # Set the cross-sectional areas from the design variable
        # values
        self.setAreas(x, lb_factor=self.epsilon)

        # Evaluate compliance objective
        self.assembleMat(self.A, self.K)
        self.assembleLoadVec(self.f)
        self.applyBCs(self.K, self.f)

        # Copy the values
        self.u[:] = self.f[:]

        # Perform the Cholesky factorization
        self.L = linalg.cholesky(self.K, lower=True)
            
        # Solve the resulting linear system of equations
        linalg.solve_triangular(self.L, self.u, lower=True,
                                trans='N', overwrite_b=True)
        linalg.solve_triangular(self.L, self.u, lower=True, 
                                trans='T', overwrite_b=True)

        # Compute the compliance objective
        obj = np.dot(self.u, self.f)
        if self.obj_scale is None:
            self.obj_scale = 1.0*obj

        # Scale the compliance objective
        obj = obj/self.obj_scale + np.dot(self.penalty, x)
                    
        # Compute the mass of the entire truss
        mass = np.dot(self.gmass, x)

        if self.use_mass_constraint:            
            # Create the constraint c(x) >= 0.0 for the mass
            con = np.array([1.0 - mass/self.m_fixed])
        else:
            # Add the deviation from the fixed mass to the objective function
            obj += 0.5*self.sigma*(mass/self.m_fixed - 1.0)**2
            con = np.array([])

        fail = 0
        return fail, obj, con

    def evalObjConGradient(self, x, gobj, Acon):
        '''
        Evaluate the derivative of the compliance and mass
        '''
        
        # Add the number of gradient evaluations
        self.gevals += 1

        # Set the areas from the design variable values
        self.setAreas(x, lb_factor=self.epsilon)

        # Zero the objecive and constraint gradients
        gobj[:] = 0.0

        # Allocate a numpy array for the gradient of the mass
        gmass = np.zeros(gobj.shape)

        # Set the number of materials
        nmats = len(self.Avals)+1
        
        # Add up the contribution to the gradient
        index = 0
        for bar in self.conn:
            # Get the first and second node numbers from the bar
            n1 = bar[0]
            n2 = bar[1]

            # Compute the nodal locations
            xd = self.xpos[2*n2] - self.xpos[2*n1]
            yd = self.xpos[2*n2+1] - self.xpos[2*n1+1]
            Le = np.sqrt(xd**2 + yd**2)
            C = xd/Le
            S = yd/Le

            # Compute the element stiffness matrix
            Ke = (self.E/Le)*np.array(
                [[C**2, C*S, -C**2, -C*S],
                 [C*S, S**2, -C*S, -S**2],
                 [-C**2, -C*S, C**2, C*S],
                 [-C*S, -S**2, C*S, S**2]])
            
            # Create a list of the element variables for convenience
            elem_vars = [2*n1, 2*n1+1, 2*n2, 2*n2+1]
            
            # Add the product to the derivative of the compliance
            g = 0.0
            for i in xrange(4):
                for j in xrange(4):
                    g -= self.u[elem_vars[i]]*self.u[elem_vars[j]]*Ke[i, j]
            
            # Add the contribution to each derivative
            for j in xrange(self.nmats):
                gobj[self.nblock*index+1+j] += g*self.Avals[j]

            # Increment the index
            index += 1

        # Scale the objective gradient
        gobj /= self.obj_scale
        gobj[:] += self.penalty

        # Check how to handle the mass constraint
        if self.use_mass_constraint:
            Acon[0, :] = -self.gmass[:]/self.m_fixed
        else:
            # Compute the mass of the truss
            mass = np.dot(self.gmass, x)

            # Add the contribution to the gradient of the mass
            gobj[:] += (self.sigma/self.m_fixed)*(mass/self.m_fixed - 1.0)*self.gmass

        fail = 0
        return fail

    def evalHvecProduct(self, x, z, zw, px, hvec):
        '''
        Evaluate the product of the input vector px with the Hessian
        of the Lagrangian.
        '''

        # Add the number of function evaluations
        self.hevals += 1
        
        # Zero the hessian-vector product
        hvec[:] = 0.0

        # Assemble the stiffness matrix along the px direction
        self.setAreas(px, lb_factor=0.0)
        self.assembleMat(self.A, self.Kp)
        np.dot(self.Kp, self.u, out=self.phi)
        self.applyBCs(self.Kp, self.phi)

        # Solve the resulting linear system of equations
        linalg.solve_triangular(self.L, self.phi, lower=True,
                                trans='N', overwrite_b=True)
        linalg.solve_triangular(self.L, self.phi, lower=True, 
                                trans='T', overwrite_b=True)
        
        # Add up the contribution to the gradient
        index = 0
        for bar in self.conn:
            # Get the first and second node numbers from the bar
            n1 = bar[0]
            n2 = bar[1]

            # Compute the nodal locations
            xd = self.xpos[2*n2] - self.xpos[2*n1]
            yd = self.xpos[2*n2+1] - self.xpos[2*n1+1]
            Le = np.sqrt(xd**2 + yd**2)
            C = xd/Le
            S = yd/Le
            
            # Compute the element stiffness matrix
            Ke = (self.E/Le)*np.array(
                [[C**2, C*S, -C**2, -C*S],
                 [C*S, S**2, -C*S, -S**2],
                 [-C**2, -C*S, C**2, C*S],
                 [-C*S, -S**2, C*S, S**2]])
            
            # Create a list of the element variables for convenience
            elem_vars = [2*n1, 2*n1+1, 2*n2, 2*n2+1]
            
            # Add the product to the derivative of the compliance
            h = 0.0
            for i in xrange(4):
                for j in xrange(4):
                    h += 2.0*self.phi[elem_vars[i]]*self.u[elem_vars[j]]*Ke[i, j]

            # Add the contribution to each derivative
            for j in xrange(self.nmats):
                hvec[self.nblock*index+1+j] += h*self.Avals[j]
            
            index += 1

        # Evaluate the derivative
        hvec /= self.obj_scale

        if not self.use_mass_constraint:
            # Add the contribution from the mass penalty term in the objective function
            hvec[:] += self.sigma*self.gmass*np.dot(self.gmass, px)/self.m_fixed**2

        fail = 0
        return fail

    def assembleMat(self, A, K):
        '''
        Given the connectivity, nodal locations and material properties,
        assemble the stiffness matrix
        
        input:
        A:   the bar areas

        output:
        K:   the stiffness matrix
        '''

        # Zero the stiffness matrix
        K[:,:] = 0.0

        # Loop over each element in the mesh
        for bar, A_bar in zip(self.conn, A):
            # Get the first and second node numbers from the bar
            n1 = bar[0]
            n2 = bar[1]

            # Compute the nodal locations
            xd = self.xpos[2*n2] - self.xpos[2*n1]
            yd = self.xpos[2*n2+1] - self.xpos[2*n1+1]
            Le = np.sqrt(xd**2 + yd**2)
            C = xd/Le
            S = yd/Le
        
            # Compute the element stiffness matrix
            Ke = (self.E*A_bar/Le)*np.array(
                [[C**2, C*S, -C**2, -C*S],
                 [C*S, S**2, -C*S, -S**2],
                 [-C**2, -C*S, C**2, C*S],
                 [-C*S, -S**2, C*S, S**2]])
        
            # Create a list of the element variables for convenience
            elem_vars = [2*n1, 2*n1+1, 2*n2, 2*n2+1]
        
            # Add the element stiffness matrix to the global stiffness
            # matrix
            for i in xrange(4):
                for j in xrange(4):
                    K[elem_vars[i], elem_vars[j]] += Ke[i, j]
                    
        return

    def assembleLoadVec(self, f):
        '''
        Create the load vector and populate the vector with entries
        '''
        
        f[:] = 0.0
        for node in self.loads:
            # Add the values to the nodal locations
            f[2*node] += self.loads[node][0]
            f[2*node+1] += self.loads[node][1]

        return

    def applyBCs(self, K, f):
        ''' 
        Apply the boundary conditions to the stiffness matrix and load
        vector
        '''

        # For each node that is in the boundary condition dictionary
        for node in self.bcs:
            uv_list = self.bcs[node]

            # For each index in the boundary conditions (corresponding to
            # either a constraint on u and/or constraint on v
            for index in uv_list:
                var = 2*node + index

                # Apply the boundary condition for the variable
                K[var, :] = 0.0
                K[:, var] = 0.0
                K[var, var] = 1.0
                f[var] = 0.0

        return

    def evalSparseCon(self, x, con):
        '''Evaluate the sparse constraints'''
        con[:] = (2.0*x[::self.nblock] - 
                  np.sum(x.reshape(-1, self.nblock), axis=1))
        return

    def addSparseJacobian(self, alpha, x, px, con):
        '''Compute the Jacobian-vector product con = alpha*J(x)*px'''
        con[:] += alpha*(2.0*px[::self.nblock] - 
                         np.sum(px.reshape(-1, self.nblock), axis=1))
        return

    def addSparseJacobianTranspose(self, alpha, x, pz, out):
        '''Compute the transpose Jacobian-vector product alpha*J^{T}*pz'''
        out[::self.nblock] += alpha*pz
        for k in xrange(1,self.nblock):
            out[k::self.nblock] -= alpha*pz
        return

    def addSparseInnerProduct(self, alpha, x, c, A):
        '''Add the results from the product J(x)*C*J(x)^{T} to A'''
        A[:] += alpha*np.sum(c.reshape(-1, self.nblock), axis=1)        
        return

    def getTikzPrefix(self):
        '''Return the file prefix'''

        s = '\\documentclass{article}\n'
        s += '\\usepackage[usenames,dvipsnames]{xcolor}\n'
        s += '\\usepackage{tikz}\n'
        s += '\\usepackage[active,tightpage]{preview}\n'
        s += '\\usepackage{amsmath}\n'
        s += '\\usepackage{helvet}\n'
        s += '\\usepackage{sansmath}\n'
        s += '\\PreviewEnvironment{tikzpicture}\n'
        s += '\\setlength\PreviewBorder{5pt}\n'
        s += '\\begin{document}\n'
        s += '\\begin{figure}\n'
        s += '\\begin{tikzpicture}[x=1cm, y=1cm]\n'
        s += '\\sffamily\n'

        return s

    def printTruss(self, x, filename='file.tex', gamma=None):
        '''Print the truss to an output file'''

        s = self.getTikzPrefix()
        bar_colors = ['Black', 'ForestGreen', 'Blue']
        
        # Get the minimum value
        Amin = 1.0*min(self.Avals)

        for i in xrange(self.nelems):
            # Get the node numbers for this element
            n1 = self.conn[i][0]
            n2 = self.conn[i][1]

            t = x[self.nblock*i]
            if t >= self.epsilon:
                for j in xrange(self.nmats):
                    xj = x[self.nblock*i+1+j]
                    if xj > self.epsilon:
                        s += '\\draw[line width=%f, color=%s, opacity=%f]'%(
                            2.0*self.Avals[j]/Amin, bar_colors[j], xj)
                        s += '(%f,%f) -- (%f,%f);\n'%(
                            self.xpos[2*n1], self.xpos[2*n1+1], 
                            self.xpos[2*n2], self.xpos[2*n2+1])

        # Show the discrete infeasibility measure...
        if gamma is not None:
            d = self.getDiscreteInfeas(x)
            U = self.getStrainEnergy(x)
            
            ymax = max(self.xpos[1::2])
            yoffset = -1.05*ymax

            for i in xrange(self.nelems):
                # Get the node numbers for this element
                n1 = self.conn[i][0]
                n2 = self.conn[i][1]
                
                t = x[self.nblock*i]
                s += '\\draw[line width=%f, color=%s, opacity=%f]'%(
                    2.0, 'black', gamma[i]/max(gamma))
                s += '(%f,%f) -- (%f,%f);\n'%(
                    self.xpos[2*n1], self.xpos[2*n1+1] + yoffset, 
                    self.xpos[2*n2], self.xpos[2*n2+1] + yoffset)

        s += '\\end{tikzpicture}'
        s += '\\end{figure}'
        s += '\\end{document}'

        # Write the file
        fp = open(filename, 'w')
        fp.write(s)
        fp.close()

        return
