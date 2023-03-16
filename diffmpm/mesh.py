import jax.numpy as jnp
from jax import vmap, lax
from diffmpm.node import Nodes
from diffmpm.particle import Particles
from diffmpm.shapefn import ShapeFn


class Mesh1D:
    """
    1D Mesh class with nodes, elements, and particles.
    """

    def __init__(
        self,
        nelements,
        material,
        domain_size,
        boundary_nodes,
        ppe=1,
        particle_distribution="uniform",
    ):
        """
        Construct a 1D Mesh.

        Arguments
        ---------
        nelements : int
            Number of elements in the mesh.
        material : diffmpm.material.Material
            Material to meshed.
        domain_size : float
            The size of the domain in consideration.
        boundary_nodes : array_like
            Node ids of boundary nodes of the mesh. Needs to be a JAX
        array.
        ppe : int
            Number of particles per element in Mesh.
        """
        self.dim = 1
        self.material = material
        self.shapefn = ShapeFn(self.dim)
        self.domain_size = domain_size
        self.nelements = nelements
        self.element_length = domain_size / nelements
        self.elements = jnp.arange(nelements)
        self.nodes = Nodes(nelements + 1)
        self.nodes.position = jnp.arange(nelements + 1) * self.element_length
        self.boundary_nodes = boundary_nodes
        self.ppe = ppe
        self.particles = self._init_particles(particle_distribution)
        return

    def _init_particles(self, distribution="uniform"):
        temp_px = jnp.linspace(0, self.element_length, self.ppe + 1)
        if distribution == "uniform":
            pmass = self.element_length * self.material.density / self.ppe
            element_particle_x = (temp_px[1:] + temp_px[:-1]) / 2
            particles_x = jnp.hstack(
                [(x + element_particle_x) for x in self.nodes.position[:-1]]
            )
            particles_xi = jnp.tile(element_particle_x, self.nelements)
            particle_element_ids = jnp.repeat(
                jnp.arange(self.nelements), self.ppe
            )
            particles = Particles(
                pmass,
                particles_x,
                particles_xi,
                self.material,
                self.material.density,
                self.ppe,
                self.nelements,
                particle_element_ids,
                self.domain_size,
            )
            return particles
        else:
            raise ValueError(
                f"{type} type particle initialization not "
                f"yet supported. Please use 'uniform'."
            )

    def _get_element_node_ids(self, element_idx):
        """
        Given an element at index `element_idx`, return the
        mapping node coordinates for that element.
        """
        return jnp.asarray([element_idx, element_idx + 1])

    def _get_element_node_pos(self, element_idx):
        """
        Given an element at index `element_idx`, return the
        mapping node coordinates for that element.
        """
        return self.nodes.position[jnp.asarray([element_idx, element_idx + 1])]

    def _get_element_node_vel(self, element_idx):
        """
        Given an element at index `element_idx`, return the
        mapping node coordinates for that element.
        """
        return self.nodes.velocity[jnp.asarray([element_idx, element_idx + 1])]

    def _update_particle_element_ids(self):
        """
        Find the element that the particles belong to.

        If the particle doesn't lie between the boundaries of any
        element, it sets the element index to -1.
        """

        def f(x):
            idl = (
                len(self.nodes.position)
                - 1
                - jnp.asarray(self.nodes.position[::-1] <= x).nonzero(
                    size=1, fill_value=-1
                )[0][-1]
            )
            idg = (
                jnp.asarray(self.nodes.position > x).nonzero(
                    size=1, fill_value=-1
                )[0][0]
                - 1
            )
            return (idl, idg)

        ids = vmap(f)(self.particles.x)
        self.particles.element_ids = jnp.where(
            ids[0] == ids[1], ids[0], jnp.ones_like(ids[0]) * -1
        )

    def _update_particle_natural_coords(self):
        r"""
        Update natural coordinates for the particles.

        Whenever the particles' physical coordinates change, their
        natural coordinates need to be updated. This function updates
        the natural coordinates of the particles based on the element
        a particle is a part of. The update formula is

        :math:`xi = (x - x_{n_0}) 2 / l - 1`

        If a particle is not in any element (element_id = -1), its
        natural coordinate is set to 0.
        """
        t = self.nodes.position[self.particles.element_ids]
        t = jnp.where(
            self.particles.element_ids == -1,
            self.particles.x - self.element_length / 2,
            t,
        )
        xi_coords = (self.particles.x - t) * 2 / self.element_length - 1
        self.particles.xi = xi_coords

    def _update_particle_strain(self, dt):
        """
        Calculate the strain values for particles.

        This calculation is done by mapping the nodal velocities
        with the gradient of the interpolation shape function.

        Arguments
        ---------
        dt : float
            Time step.
        """

        nodal_coords = vmap(self._get_element_node_pos)(
            self.particles.element_ids
        )
        # particles_dndx will be of shape (nparticles, element.nnodes)
        particles_dndx = vmap(self.shapefn.shapefn_grad)(
            self.particles.xi, nodal_coords
        )
        nodal_vel = vmap(self._get_element_node_vel)(self.particles.element_ids)

        # strain rate is the row-wise sum of the matrix particles_dndx x nodal_vel
        strain_rate = jnp.sum(particles_dndx * nodal_vel, axis=1)

        self.particles.dstrain = strain_rate * dt
        self.particles.strain += self.particles.dstrain

    def _update_particle_stress(self):
        self.particles.stress += self.particles.dstrain * self.material.E

    def _update_nodes_acc_vel(self, dt):
        """
        Compute acceleration based velocity.

        The velocity at nodes is calculated based on the acceleration
        achieved by the force on the nodes and added to the current
        velocity. For velocity update using momentum see
        `diffmpm.Mesh._update_nodes_mom_vel()`

        Arguments
        ---------
        dt : float
            Time step.
        """
        total_force = self.nodes.get_total_force()

        def f(f, m):
            nodal_acceleration = lax.cond(
                m == 0,
                lambda cf, cm: 0.0,
                lambda cf, cm: jnp.divide(cf, cm),
                f,
                m,
            )
            return nodal_acceleration

        nodal_acceleration = vmap(f)(total_force, self.nodes.mass)
        self.nodes.velocity += nodal_acceleration * dt

    def _update_nodes_mom_vel(self):
        """
        Compute momentum based velocity.

        The velocity of the nodes is calculated based on the current
        momentum at the nodes. This function _sets_ the value of the
        velocities for each node. For acceleration based update see
        `diffmpm.Mesh._update_nodes_acc_vel()`
        """

        def f(p, m):
            velocity = lax.cond(
                m == 0,
                lambda cp, cm: jnp.zeros_like(cp),
                lambda cp, cm: jnp.divide(cp, cm),
                p,
                m,
            )
            return velocity

        nodal_velocity = vmap(f)(self.nodes.momentum, self.nodes.mass)
        self.nodes.velocity = nodal_velocity

    def _update_nodes_bc_mom_vel(self):
        """
        Set momentum and velocity of boundary nodes.

        Based on the boundary conditions of the mesh, the nodes at the
        boundary points are set to 0 momentum and velocity.
        """
        self.nodes.momentum = self.nodes.momentum.at[self.boundary_nodes].set(0)
        self.nodes.velocity = self.nodes.velocity.at[self.boundary_nodes].set(0)

    def _update_nodes_bc_force(self):
        """
        Set forces of boundary nodes.

        Based on the boundary conditions of the mesh, the forces on the
        nodes at the boundary points are set to 0.
        """
        self.nodes.f_int = self.nodes.f_int.at[self.boundary_nodes].set(0)
        self.nodes.f_ext = self.nodes.f_ext.at[self.boundary_nodes].set(0)
        self.nodes.f_damp = self.nodes.f_damp.at[self.boundary_nodes].set(0)

    def _update_node_momentum_force(self, dt):
        """
        Update the momentum at nodes based on force

        :math:`p += total_force * dt`

        Arguments
        ---------
        dt : float
            Time step.
        """
        self.nodes.momentum += (
            self.nodes.f_int + self.nodes.f_ext + self.nodes.f_damp
        ) * dt

    def _update_node_momentum_par_vel(self):
        r"""
        Update the nodal momentum based on particle velocity.

        The nodal momentum is updated as a sum of particle momentum for
        all particles mapped to the node.

        :math:`(mv)_i = \sum_p N_i(x_p) m_p v_p`
        """
        self.nodes.momentum = self.nodes.momentum.at[:].set(0)

        def step(pid, args):
            momentum, mass, velocity, mapped_pos, el_nodes = args
            momentum = momentum.at[el_nodes[pid]].add(
                mass[pid] * velocity[pid] * mapped_pos[pid]
            )
            return momentum, mass, velocity, mapped_pos, el_nodes

        mapped_positions = self.shapefn.shapefn(self.particles.xi)
        mapped_nodes = vmap(self._get_element_node_ids)(
            self.particles.element_ids
        )
        args = (
            self.nodes.momentum,
            self.particles.mass,
            self.particles.velocity,
            mapped_positions,
            mapped_nodes,
        )
        self.nodes.momentum, _, _, _, _ = lax.fori_loop(
            0, len(self.particles), step, args
        )

    def _transfer_node_force_vel_par(self, dt):
        """
        Transfer nodal velocity to particles.

        The velocity is calculated based on the total force at nodes.

        Arguments
        ---------
        dt : float
            Timestep.
        """
        mapped_positions = self.shapefn.shapefn(self.particles.xi)
        mapped_ids = vmap(self._get_element_node_ids)(
            self.particles.element_ids
        )
        total_force = self.nodes.get_total_force()
        self.particles.velocity = self.particles.velocity.at[:].add(
            jnp.sum(
                mapped_positions
                * jnp.divide(
                    total_force[mapped_ids], self.nodes.mass[mapped_ids]
                )
                * dt,
                axis=1,
            )
        )

    def _update_par_pos_node_mom(self, dt):
        """
        Update particle position based on nodal momentum.

        Arguments
        ---------
        dt : float
            Time step.
        """
        mapped_positions = self.shapefn.shapefn(self.particles.xi)
        mapped_ids = vmap(self._get_element_node_ids)(
            self.particles.element_ids
        )
        self.particles.x = self.particles.x.at[:].add(
            jnp.sum(
                mapped_positions
                * jnp.divide(
                    self.nodes.momentum[mapped_ids], self.nodes.mass[mapped_ids]
                )
                * dt,
                axis=1,
            )
        )

    def _update_par_pos_vel_node_vel(self, dt):
        """
        Update particle position and velocity based on nodal velocity.

        Arguments
        ---------
        dt : float
            Timestep.
        """
        mapped_positions = self.shapefn.shapefn(self.particles.xi)
        mapped_vel = vmap(self._get_element_node_vel)(
            self.particles.element_ids
        )
        self.particles.velocity = self.particles.velocity.at[:].set(
            jnp.sum(
                mapped_positions * mapped_vel,
                axis=1,
            )
        )
        self.particles.x = self.particles.x.at[:].add(
            self.particles.velocity * dt
        )

    def _update_par_vol_density(self):
        """
        Update the particle volume and density based on dstrain.
        """
        self.particles.volume = self.particles.volume.at[:].multiply(
            1 + self.particles.dstrain
        )
        self.particles.density = self.particles.density.at[:].divide(
            1 + self.particles.dstrain
        )

    def _update_node_mass_par_mass(self):
        r"""
        Update the nodal mass based on particle mass.

        The nodal mass is updated as a sum of particle mass for
        all particles mapped to the node.

        :math:`(m)_i = \sum_p N_i(x_p) m_p`
        """

        def step(pid, args):
            pmass, mass, mapped_pos, el_nodes = args
            mass = mass.at[el_nodes[pid]].add(pmass[pid] * mapped_pos[pid])
            return pmass, mass, mapped_pos, el_nodes

        mapped_positions = self.shapefn.shapefn(self.particles.xi)
        mapped_nodes = vmap(self._get_element_node_ids)(
            self.particles.element_ids
        )
        args = (
            self.particles.mass,
            self.nodes.mass,
            mapped_positions,
            mapped_nodes,
        )
        _, self.nodes.mass, _, _ = lax.fori_loop(
            0, len(self.particles), step, args
        )

    def _update_node_fext_par_mass(self, gravity):
        r"""
        Update the nodal external force based on particle mass.

        The nodal force is updated as a sum of particle weight for
        all particles mapped to the node.

        :math:`(f_{ext})_i = \sum_p N_i(x_p) m_p g`
        """

        def step(pid, args):
            f_ext, pmass, mapped_pos, el_nodes, gravity = args
            f_ext = f_ext.at[el_nodes[pid]].add(
                pmass[pid] * mapped_pos[pid] * gravity
            )
            return f_ext, pmass, mapped_pos, el_nodes, gravity

        mapped_positions = self.shapefn.shapefn(self.particles.xi)
        mapped_nodes = vmap(self._get_element_node_ids)(
            self.particles.element_ids
        )
        args = (
            self.nodes.f_ext,
            self.particles.mass,
            mapped_positions,
            mapped_nodes,
            gravity,
        )
        self.nodes.f_ext, _, _, _, _ = lax.fori_loop(
            0, len(self.particles), step, args
        )

    def _update_node_fint_par_mass(self):
        r"""
        Update the nodal internal force based on particle mass.

        The nodal force is updated as a sum of internal forces for
        all particles mapped to the node.

        :math:`(mv)_i = \sum_p N_i(x_p) * stress * m_p / density_p`
        """

        def step(pid, args):
            (
                f_int,
                pmass,
                mapped_grads,
                el_nodes,
                pstress,
                pdensity,
            ) = args
            f_int = f_int.at[el_nodes[pid]].add(
                -pmass[pid] * mapped_grads[pid] * pstress[pid] / pdensity[pid]
            )
            return (
                f_int,
                pmass,
                mapped_grads,
                el_nodes,
                pstress,
                pdensity,
            )

        mapped_nodes = vmap(self._get_element_node_ids)(
            self.particles.element_ids
        )
        mapped_grads = vmap(self.shapefn.shapefn_grad)(
            self.particles.x, mapped_nodes
        )
        args = (
            self.nodes.f_int,
            self.particles.mass,
            mapped_grads,
            mapped_nodes,
            self.particles.stress,
            self.particles.density,
        )
        self.nodes.f_int, _, _, _, _, _ = lax.fori_loop(
            0, len(self.particles), step, args
        )

    def _update_node_fext_par_fext(self):
        r"""
        Update the nodal external force based on particle f_ext.

        The nodal force is updated as a sum of particle external
        force for all particles mapped to the node.

        :math:`(mv)_i = \sum_p N_i(x_p) fext`
        """

        def step(pid, args):
            f_ext, pf_ext, mapped_pos, el_nodes = args
            f_ext = f_ext.at[el_nodes[pid]].add(mapped_pos[pid] * pf_ext[pid])
            return f_ext, pf_ext, mapped_pos, el_nodes

        mapped_positions = self.shapefn.shapefn(self.particles.xi)
        mapped_nodes = vmap(self._get_element_node_ids)(
            self.particles.element_ids
        )
        args = (
            self.nodes.f_ext,
            self.particles.f_ext,
            mapped_positions,
            mapped_nodes,
        )
        self.nodes.f_ext, _, _, _ = lax.fori_loop(
            0, len(self.particles), step, args
        )

    def solve(self, **kwargs):
        """Solve the mesh using explicit scheme (for now)."""
        # TODO: Add flow control and argument checking
        result = {"position": [], "velocity": []}
        b1 = jnp.pi * 0.5 / self.domain_size
        self.particles.velocity = 0.1 * jnp.sin(b1 * self.particles.x)
        for step in range(kwargs["nsteps"]):
            self._update_particle_natural_coords()
            self._update_particle_element_ids()
            self._update_node_momentum_par_vel()
            self._update_node_mass_par_mass()
            self._update_nodes_bc_mom_vel()
            if kwargs["mpm_scheme"] == "USF":
                self._update_nodes_mom_vel()
                self._update_particle_strain(kwargs["dt"])
                self._update_par_vol_density()
                self._update_particle_stress()

            self._update_node_fint_par_mass()
            self._update_node_fext_par_fext()
            self._update_nodes_bc_force()
            self._update_node_momentum_force(kwargs["dt"])
            self._transfer_node_force_vel_par(kwargs["dt"])
            self._update_par_pos_node_mom(kwargs["dt"])
            if kwargs["mpm_scheme"] == "MUSL":
                self._update_node_momentum_par_vel()
                self._update_nodes_bc_mom_vel()

            if kwargs["mpm_scheme"] == "MUSL" or kwargs["mpm_scheme"] == "USL":
                self._update_nodes_mom_vel()
                self._update_particle_strain(kwargs["dt"])
                self._update_par_vol_density()
                self._update_particle_stress()
            self.nodes.reset_values()
            result["position"].append(self.particles.x)
            result["velocity"].append(self.particles.velocity)
        return result
