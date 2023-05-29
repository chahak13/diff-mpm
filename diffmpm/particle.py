from typing import Tuple

import jax.numpy as jnp
from jax import jit, vmap
from jax.tree_util import register_pytree_node_class

from diffmpm.element import _Element
from diffmpm.material import Material


@register_pytree_node_class
class Particles:
    """Container class for a set of particles."""

    def __init__(
        self,
        loc: jnp.ndarray,
        material: Material,
        element_ids: jnp.ndarray,
        initialized: bool = None,
        data: Tuple[jnp.ndarray, ...] = None,
    ):
        """
        Initialize a container of particles.

        Arguments
        ---------
        loc: jax.numpy.ndarray
            Location of the particles. Expected shape (nparticles, 1, ndim)
        material: diffmpm.material.Material
            Type of material for the set of particles.
        element_ids: jax.numpy.ndarray
            The element ids that the particles belong to. This contains
        information that will make sense only with the information of
        the mesh that is being considered.
        initialized: bool
            False if particle property arrays like mass need to be initialized.
        If True, they are set to values from `data`.
        data: tuple
            Tuple of length 13 that sets arrays for mass, density, volume,
        velocity, acceleration, momentum, strain, stress, strain_rate,
        dstrain, f_ext, reference_loc and volumetric_strain_centroid.
        """
        self.material = material
        self.element_ids = element_ids
        if len(loc.shape) != 3:
            raise ValueError(
                f"`loc` should be of size (nparticles, 1, ndim); "
                f"found {loc.shape}"
            )
        self.loc = loc

        if initialized is None:
            self.mass = jnp.zeros((self.loc.shape[0], 1, 1))
            self.density = jnp.zeros_like(self.mass)
            self.volume = jnp.zeros_like(self.mass)
            self.velocity = jnp.zeros_like(self.loc)
            self.acceleration = jnp.zeros_like(self.loc)
            self.momentum = jnp.zeros_like(self.loc)
            self.strain = jnp.zeros((self.loc.shape[0], 6, 1))
            self.stress = jnp.zeros((self.loc.shape[0], 6, 1))
            self.strain_rate = jnp.zeros((self.loc.shape[0], 6, 1))
            self.dstrain = jnp.zeros((self.loc.shape[0], 6, 1))
            self.f_ext = jnp.zeros_like(self.loc)
            self.reference_loc = jnp.zeros_like(self.loc)
            self.volumetric_strain_centroid = jnp.zeros((self.loc.shape[0], 1))
        else:
            (
                self.mass,
                self.density,
                self.volume,
                self.velocity,
                self.acceleration,
                self.momentum,
                self.strain,
                self.stress,
                self.strain_rate,
                self.dstrain,
                self.f_ext,
                self.reference_loc,
                self.volumetric_strain_centroid,
            ) = data
        self.initialized = True

    def tree_flatten(self):
        children = (
            self.loc,
            self.element_ids,
            self.initialized,
            self.mass,
            self.density,
            self.volume,
            self.velocity,
            self.acceleration,
            self.momentum,
            self.strain,
            self.stress,
            self.strain_rate,
            self.dstrain,
            self.f_ext,
            self.reference_loc,
            self.volumetric_strain_centroid,
        )
        aux_data = (self.material,)
        return (children, aux_data)

    @classmethod
    def tree_unflatten(cls, aux_data, children):
        return cls(
            children[0],
            aux_data[0],
            children[1],
            initialized=children[2],
            data=children[3:],
        )

    def __len__(self):
        """Set length of the class as number of particles."""
        return self.loc.shape[0]

    def __repr__(self):
        """Informative repr showing number of particles."""
        return f"Particles(nparticles={len(self)})"

    def set_mass_volume(self, m: float | jnp.ndarray):
        """
        Set particle mass.

        Arguments
        ---------
        m: float, array_like
            Mass to be set for particles. If scalar, mass for all
        particles is set to this value.
        """
        if jnp.isscalar(m):
            self.mass = jnp.ones_like(self.loc) * m
        elif m.shape == self.mass.shape:
            self.mass = m
        else:
            raise ValueError(
                f"Incompatible shapes. Expected {self.mass.shape}, "
                f"found {m.shape}."
            )
        self.volume = jnp.divide(self.mass, self.material.properties["density"])

    # TODO: This needs to be in element so that each type of element
    # can govern how ot check if particle is in an element.
    # This implementation is true only for 1D.
    def set_particle_element_ids(self, elements: _Element):
        """
        Set the element IDs for the particles.

        If the particle doesn't lie between the boundaries of any
        element, it sets the element index to -1.
        """

        @jit
        def f(x):
            idl = (
                len(elements.nodes.loc)
                - 1
                - jnp.asarray(elements.nodes.loc[::-1] <= x).nonzero(
                    size=1, fill_value=-1
                )[0][-1]
            )
            idg = (
                jnp.asarray(elements.nodes.loc > x).nonzero(
                    size=1, fill_value=-1
                )[0][0]
                - 1
            )
            return (idl, idg)

        ids = vmap(f)(self.loc)
        self.element_ids = jnp.where(
            ids[0] == ids[1], ids[0], jnp.ones_like(ids[0]) * -1
        )

    def update_position_velocity(self, elements: _Element, dt: float):
        """
        Transfer nodal velocity to particles and update particle position.

        The velocity is calculated based on the total force at nodes.

        Arguments
        ---------
        elements: diffmpm.element._Element
            Elements whose nodes are used to transfer the velocity.
        dt : float
            Timestep.
        """
        mapped_positions = elements.shapefn(self.reference_loc)
        mapped_ids = vmap(elements.id_to_node_ids)(self.element_ids).squeeze(-1)
        total_force = elements.nodes.get_total_force()
        self.velocity = self.velocity.at[:].add(
            jnp.sum(
                mapped_positions
                * jnp.divide(
                    total_force[mapped_ids], elements.nodes.mass[mapped_ids]
                )
                * dt,
                axis=1,
            )
        )
        self.loc = self.loc.at[:].add(
            jnp.sum(
                mapped_positions
                * jnp.divide(
                    elements.nodes.momentum[mapped_ids],
                    elements.nodes.mass[mapped_ids],
                )
                * dt,
                axis=1,
            )
        )
        self.momentum = self.momentum.at[:].set(self.mass * self.velocity)

    def compute_strain(self, elements: _Element, dt: float):
        """
        Compute the strain on all particles.

        This is done by first calculating the strain rate for the particles
        and then calculating strain as strain += strain rate * dt.

        Arguments
        ---------
        elements: diffmpm.element._Element
            Elements whose nodes are used to calculate the strain.
        dt : float
            Timestep.
        """
        mapped_coords = elements.id_to_node_loc(self.element_ids).squeeze(-1)
        dn_dx_ = vmap(elements.shapefn_grad)(
            self.reference_loc[:, jnp.newaxis, ...], mapped_coords
        )
        self.strain_rate = self._compute_strain_rate(dn_dx_, elements)
        self.dstrain = self.dstrain.at[:].set(self.strain_rate * dt)
        self.strain = self.strain.at[:].add(self.dstrain)
        centroids = jnp.zeros_like(self.loc)
        dn_dx_centroid_ = vmap(elements.shapefn_grad)(
            centroids[:, jnp.newaxis, ...], mapped_coords
        )
        strain_rate_centroid = self._compute_strain_rate(
            dn_dx_centroid_, elements
        )
        ndim = self.loc.shape[-1]
        self.dvolumetric_strain = dt * strain_rate_centroid[:, :ndim].sum(
            axis=1
        )
        self.volumetric_strain_centroid = self.volumetric_strain_centroid.at[
            :
        ].add(self.dvolumetric_strain)

    def _compute_strain_rate(self, dn_dx: jnp.ndarray, elements: _Element):
        """
        Compute the strain rate for particles.

        Arguments
        ---------
        dn_dx: jnp.ndarray
            The gradient of the shape function.
        Expected shape (nparticles, 1, ndim)
        elements: diffmpm.element._Element
            Elements whose nodes are used to calculate the strain rate.
        """
        strain_rate = jnp.zeros((dn_dx.shape[0], 6, 1))  # (nparticles, 6, 1)
        mapped_vel = vmap(elements.id_to_node_vel)(
            self.element_ids
        )  # (nparticles, 2, 1)

        # TODO: This will need to change to be more general for ndim.
        L = jnp.einsum("ijk, ikj -> ijk", dn_dx, mapped_vel).sum(axis=2)
        strain_rate = strain_rate.at[:, 0, :].add(L)
        return strain_rate

    def compute_stress(self, *args):
        """
        Compute the strain on all particles.

        This calculation is governed by the material of the
        particles. The stress calculated by the material is then
        added to the particles current stress values.
        """
        self.stress = self.stress.at[:].add(
            self.material.compute_stress(self.dstrain)
        )

    def update_volume(self):
        """Update volume based on central strain rate."""
        self.volume = self.volume.at[:].multiply(1 + self.dvolumetric_strain)
        self.density = self.density.at[:].divide(1 + self.dvolumetric_strain)


if __name__ == "__main__":
    from diffmpm.material import SimpleMaterial
    from diffmpm.utils import _show_example

    _show_example(
        Particles(
            jnp.array([[[1]]]),
            SimpleMaterial({"E": 2, "density": 1}),
            jnp.array([0]),
        )
    )
