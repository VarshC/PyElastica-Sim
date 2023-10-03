__doc__ = """ Numba implementation module containing contact between rods and rigid bodies and other rods rigid bodies or surfaces."""

from elastica.typing import RodType, SystemType, AllowedContactType
from elastica.rod import RodBase
from elastica.rigidbody import Cylinder, Sphere
from elastica.contact_utils import (
    _dot_product,
    _norm,
    _find_min_dist,
    _prune_using_aabbs_rod_cylinder,
    _prune_using_aabbs_rod_rod,
    _prune_using_aabbs_rod_sphere,
)

from elastica._linalg import _batch_product_k_ik_to_ik
import numba
import numpy as np


class NoContact:
    """
    This is the base class for contact applied between rod-like objects and allowed contact objects.

    Notes
    -----
    Every new contact class must be derived
    from NoContact class.

    """

    def __init__(self):
        """
        NoContact class does not need any input parameters.
        """

    def _check_systems_validity(
        self,
        system_one: SystemType,
        system_two: AllowedContactType,
    ) -> None:
        """
        This checks the contact order between a SystemType object and an AllowedContactType object, the order should follow: Rod, Rigid body, Surface.
        In NoContact class, this just checks if system_two is a rod then system_one must be a rod.


        Parameters
        ----------
        system_one
            SystemType
        system_two
            AllowedContactType
        """
        if issubclass(system_two.__class__, RodBase):
            if not issubclass(system_one.__class__, RodBase):
                raise TypeError(
                    "Systems provided to the contact class have incorrect order. \n"
                    " First system is {0} and second system is {1}. \n"
                    " If the first system is a rod, the second system can be a rod, rigid body or surface. \n"
                    " If the first system is a rigid body, the second system can be a rigid body or surface.".format(
                        system_one.__class__, system_two.__class__
                    )
                )

    def apply_contact(
        self,
        system_one: SystemType,
        system_two: AllowedContactType,
    ) -> None:
        """
        Apply contact forces and torques between SystemType object and AllowedContactType object.

        In NoContact class, this routine simply passes.

        Parameters
        ----------
        system_one : SystemType
            Rod or rigid-body object
        system_two : AllowedContactType
            Rod, rigid-body, or surface object
        """
        pass


@numba.njit(cache=True)
def _calculate_contact_forces_rod_cylinder(
    x_collection_rod,
    edge_collection_rod,
    x_cylinder_center,
    x_cylinder_tip,
    edge_cylinder,
    radii_sum,
    length_sum,
    internal_forces_rod,
    external_forces_rod,
    external_forces_cylinder,
    external_torques_cylinder,
    cylinder_director_collection,
    velocity_rod,
    velocity_cylinder,
    contact_k,
    contact_nu,
    velocity_damping_coefficient,
    friction_coefficient,
) -> None:
    # We already pass in only the first n_elem x
    n_points = x_collection_rod.shape[1]
    cylinder_total_contact_forces = np.zeros((3))
    cylinder_total_contact_torques = np.zeros((3))
    for i in range(n_points):
        # Element-wise bounding box
        x_selected = x_collection_rod[..., i]
        # x_cylinder is already a (,) array from outised
        del_x = x_selected - x_cylinder_tip
        norm_del_x = _norm(del_x)

        # If outside then don't process
        if norm_del_x >= (radii_sum[i] + length_sum[i]):
            continue

        # find the shortest line segment between the two centerline
        # segments : differs from normal cylinder-cylinder intersection
        distance_vector, x_cylinder_contact_point, _ = _find_min_dist(
            x_selected, edge_collection_rod[..., i], x_cylinder_tip, edge_cylinder
        )
        distance_vector_length = _norm(distance_vector)
        distance_vector /= distance_vector_length

        gamma = radii_sum[i] - distance_vector_length

        # If distance is large, don't worry about it
        if gamma < -1e-5:
            continue

        # CHECK FOR GAMMA > 0.0, heaviside but we need to overload it in numba
        # As a quick fix, use this instead
        mask = (gamma > 0.0) * 1.0

        # Compute contact spring force
        contact_force = contact_k * gamma * distance_vector
        interpenetration_velocity = velocity_cylinder[..., 0] - 0.5 * (
            velocity_rod[..., i] + velocity_rod[..., i + 1]
        )
        # Compute contact damping
        normal_interpenetration_velocity = (
            _dot_product(interpenetration_velocity, distance_vector) * distance_vector
        )
        contact_damping_force = -contact_nu * normal_interpenetration_velocity

        # magnitude* direction
        net_contact_force = 0.5 * mask * (contact_damping_force + contact_force)

        # Compute friction
        slip_interpenetration_velocity = (
            interpenetration_velocity - normal_interpenetration_velocity
        )
        slip_interpenetration_velocity_mag = np.linalg.norm(
            slip_interpenetration_velocity
        )
        slip_interpenetration_velocity_unitized = slip_interpenetration_velocity / (
            slip_interpenetration_velocity_mag + 1e-14
        )
        # Compute friction force in the slip direction.
        damping_force_in_slip_direction = (
            velocity_damping_coefficient * slip_interpenetration_velocity_mag
        )
        # Compute Coulombic friction
        coulombic_friction_force = friction_coefficient * np.linalg.norm(
            net_contact_force
        )
        # Compare damping force in slip direction and kinetic friction and minimum is the friction force.
        friction_force = (
            -min(damping_force_in_slip_direction, coulombic_friction_force)
            * slip_interpenetration_velocity_unitized
        )
        # Update contact force
        net_contact_force += friction_force

        # Torques acting on the cylinder
        moment_arm = x_cylinder_contact_point - x_cylinder_center

        # Add it to the rods at the end of the day
        if i == 0:
            external_forces_rod[..., i] -= 2 / 3 * net_contact_force
            external_forces_rod[..., i + 1] -= 4 / 3 * net_contact_force
            cylinder_total_contact_forces += 2.0 * net_contact_force
            cylinder_total_contact_torques += np.cross(
                moment_arm, 2.0 * net_contact_force
            )
        elif i == n_points - 1:
            external_forces_rod[..., i] -= 4 / 3 * net_contact_force
            external_forces_rod[..., i + 1] -= 2 / 3 * net_contact_force
            cylinder_total_contact_forces += 2.0 * net_contact_force
            cylinder_total_contact_torques += np.cross(
                moment_arm, 2.0 * net_contact_force
            )
        else:
            external_forces_rod[..., i] -= net_contact_force
            external_forces_rod[..., i + 1] -= net_contact_force
            cylinder_total_contact_forces += 2.0 * net_contact_force
            cylinder_total_contact_torques += np.cross(
                moment_arm, 2.0 * net_contact_force
            )

    # Update the cylinder external forces and torques
    external_forces_cylinder[..., 0] += cylinder_total_contact_forces
    external_torques_cylinder[..., 0] += (
        cylinder_director_collection @ cylinder_total_contact_torques
    )


@numba.njit(cache=True)
def _calculate_contact_forces_rod_rod(
    x_collection_rod_one,
    radius_rod_one,
    length_rod_one,
    tangent_rod_one,
    velocity_rod_one,
    internal_forces_rod_one,
    external_forces_rod_one,
    x_collection_rod_two,
    radius_rod_two,
    length_rod_two,
    tangent_rod_two,
    velocity_rod_two,
    internal_forces_rod_two,
    external_forces_rod_two,
    contact_k,
    contact_nu,
) -> None:
    # We already pass in only the first n_elem x
    n_points_rod_one = x_collection_rod_one.shape[1]
    n_points_rod_two = x_collection_rod_two.shape[1]
    edge_collection_rod_one = _batch_product_k_ik_to_ik(length_rod_one, tangent_rod_one)
    edge_collection_rod_two = _batch_product_k_ik_to_ik(length_rod_two, tangent_rod_two)

    for i in range(n_points_rod_one):
        for j in range(n_points_rod_two):
            radii_sum = radius_rod_one[i] + radius_rod_two[j]
            length_sum = length_rod_one[i] + length_rod_two[j]
            # Element-wise bounding box
            x_selected_rod_one = x_collection_rod_one[..., i]
            x_selected_rod_two = x_collection_rod_two[..., j]

            del_x = x_selected_rod_one - x_selected_rod_two
            norm_del_x = _norm(del_x)

            # If outside then don't process
            if norm_del_x >= (radii_sum + length_sum):
                continue

            # find the shortest line segment between the two centerline
            # segments : differs from normal cylinder-cylinder intersection
            distance_vector, _, _ = _find_min_dist(
                x_selected_rod_one,
                edge_collection_rod_one[..., i],
                x_selected_rod_two,
                edge_collection_rod_two[..., j],
            )
            distance_vector_length = _norm(distance_vector)
            distance_vector /= distance_vector_length
            gamma = radii_sum - distance_vector_length

            # If distance is large, don't worry about it
            if gamma < -1e-5:
                continue

            rod_one_elemental_forces = 0.5 * (
                external_forces_rod_one[..., i]
                + external_forces_rod_one[..., i + 1]
                + internal_forces_rod_one[..., i]
                + internal_forces_rod_one[..., i + 1]
            )

            rod_two_elemental_forces = 0.5 * (
                external_forces_rod_two[..., j]
                + external_forces_rod_two[..., j + 1]
                + internal_forces_rod_two[..., j]
                + internal_forces_rod_two[..., j + 1]
            )

            equilibrium_forces = -rod_one_elemental_forces + rod_two_elemental_forces

            """FIX ME: Remove normal force and tune rod-rod contact example"""
            normal_force = _dot_product(equilibrium_forces, distance_vector)
            # Following line same as np.where(normal_force < 0.0, -normal_force, 0.0)
            normal_force = abs(min(normal_force, 0.0))

            # CHECK FOR GAMMA > 0.0, heaviside but we need to overload it in numba
            # As a quick fix, use this instead
            mask = (gamma > 0.0) * 1.0

            contact_force = contact_k * gamma
            interpenetration_velocity = 0.5 * (
                (velocity_rod_one[..., i] + velocity_rod_one[..., i + 1])
                - (velocity_rod_two[..., j] + velocity_rod_two[..., j + 1])
            )
            contact_damping_force = contact_nu * _dot_product(
                interpenetration_velocity, distance_vector
            )

            # magnitude* direction
            net_contact_force = (
                normal_force + 0.5 * mask * (contact_damping_force + contact_force)
            ) * distance_vector

            # Add it to the rods at the end of the day
            if i == 0:
                external_forces_rod_one[..., i] -= net_contact_force * 2 / 3
                external_forces_rod_one[..., i + 1] -= net_contact_force * 4 / 3
            elif i == n_points_rod_one - 1:
                external_forces_rod_one[..., i] -= net_contact_force * 4 / 3
                external_forces_rod_one[..., i + 1] -= net_contact_force * 2 / 3
            else:
                external_forces_rod_one[..., i] -= net_contact_force
                external_forces_rod_one[..., i + 1] -= net_contact_force

            if j == 0:
                external_forces_rod_two[..., j] += net_contact_force * 2 / 3
                external_forces_rod_two[..., j + 1] += net_contact_force * 4 / 3
            elif j == n_points_rod_two - 1:
                external_forces_rod_two[..., j] += net_contact_force * 4 / 3
                external_forces_rod_two[..., j + 1] += net_contact_force * 2 / 3
            else:
                external_forces_rod_two[..., j] += net_contact_force
                external_forces_rod_two[..., j + 1] += net_contact_force


@numba.njit(cache=True)
def _calculate_contact_forces_self_rod(
    x_collection_rod,
    radius_rod,
    length_rod,
    tangent_rod,
    velocity_rod,
    external_forces_rod,
    contact_k,
    contact_nu,
) -> None:
    # We already pass in only the first n_elem x
    n_points_rod = x_collection_rod.shape[1]
    edge_collection_rod_one = _batch_product_k_ik_to_ik(length_rod, tangent_rod)

    for i in range(n_points_rod):
        skip = int(1 + np.ceil(0.8 * np.pi * radius_rod[i] / length_rod[i]))
        for j in range(i - skip, -1, -1):
            radii_sum = radius_rod[i] + radius_rod[j]
            length_sum = length_rod[i] + length_rod[j]
            # Element-wise bounding box
            x_selected_rod_index_i = x_collection_rod[..., i]
            x_selected_rod_index_j = x_collection_rod[..., j]

            del_x = x_selected_rod_index_i - x_selected_rod_index_j
            norm_del_x = _norm(del_x)

            # If outside then don't process
            if norm_del_x >= (radii_sum + length_sum):
                continue

            # find the shortest line segment between the two centerline
            # segments : differs from normal cylinder-cylinder intersection
            distance_vector, _, _ = _find_min_dist(
                x_selected_rod_index_i,
                edge_collection_rod_one[..., i],
                x_selected_rod_index_j,
                edge_collection_rod_one[..., j],
            )
            distance_vector_length = _norm(distance_vector)
            distance_vector /= distance_vector_length

            gamma = radii_sum - distance_vector_length

            # If distance is large, don't worry about it
            if gamma < -1e-5:
                continue

            # CHECK FOR GAMMA > 0.0, heaviside but we need to overload it in numba
            # As a quick fix, use this instead
            mask = (gamma > 0.0) * 1.0

            contact_force = contact_k * gamma
            interpenetration_velocity = 0.5 * (
                (velocity_rod[..., i] + velocity_rod[..., i + 1])
                - (velocity_rod[..., j] + velocity_rod[..., j + 1])
            )
            contact_damping_force = contact_nu * _dot_product(
                interpenetration_velocity, distance_vector
            )

            # magnitude* direction
            net_contact_force = (
                0.5 * mask * (contact_damping_force + contact_force)
            ) * distance_vector

            # Add it to the rods at the end of the day
            # if i == 0:
            #     external_forces_rod[...,i] -= net_contact_force *2/3
            #     external_forces_rod[...,i+1] -= net_contact_force * 4/3
            if i == n_points_rod - 1:
                external_forces_rod[..., i] -= net_contact_force * 4 / 3
                external_forces_rod[..., i + 1] -= net_contact_force * 2 / 3
            else:
                external_forces_rod[..., i] -= net_contact_force
                external_forces_rod[..., i + 1] -= net_contact_force

            if j == 0:
                external_forces_rod[..., j] += net_contact_force * 2 / 3
                external_forces_rod[..., j + 1] += net_contact_force * 4 / 3
            # elif j == n_points_rod:
            #     external_forces_rod[..., j] += net_contact_force * 4/3
            #     external_forces_rod[..., j+1] += net_contact_force * 2/3
            else:
                external_forces_rod[..., j] += net_contact_force
                external_forces_rod[..., j + 1] += net_contact_force


def _calculate_contact_forces_rod_sphere(
    x_collection_rod,
    edge_collection_rod,
    x_sphere_center,
    x_sphere_tip,
    edge_sphere,
    radii_sum,
    length_sum,
    internal_forces_rod,
    external_forces_rod,
    external_forces_sphere,
    external_torques_sphere,
    sphere_director_collection,
    velocity_rod,
    velocity_sphere,
    contact_k,
    contact_nu,
    velocity_damping_coefficient,
    friction_coefficient,
) -> None:
    # We already pass in only the first n_elem x
    n_points = x_collection_rod.shape[1]
    sphere_total_contact_forces = np.zeros((3))
    sphere_total_contact_torques = np.zeros((3))
    for i in range(n_points):
        # Element-wise bounding box
        x_selected = x_collection_rod[..., i]
        # x_sphere is already a (,) array from outside
        del_x = x_selected - x_sphere_tip
        norm_del_x = _norm(del_x)

        # If outside then don't process
        if norm_del_x >= (radii_sum[i] + length_sum[i]):
            continue

        # find the shortest line segment between the two centerline
        distance_vector, x_sphere_contact_point, _ = _find_min_dist(
            x_selected, edge_collection_rod[..., i], x_sphere_tip, edge_sphere
        )
        distance_vector_length = _norm(distance_vector)
        distance_vector /= distance_vector_length

        gamma = radii_sum[i] - distance_vector_length

        # If distance is large, don't worry about it
        if gamma < -1e-5:
            continue

        # CHECK FOR GAMMA > 0.0, heaviside but we need to overload it in numba
        # As a quick fix, use this instead
        mask = (gamma > 0.0) * 1.0

        # Compute contact spring force
        contact_force = contact_k * gamma * distance_vector
        interpenetration_velocity = velocity_sphere[..., 0] - 0.5 * (
            velocity_rod[..., i] + velocity_rod[..., i + 1]
        )
        # Compute contact damping
        normal_interpenetration_velocity = (
            _dot_product(interpenetration_velocity, distance_vector) * distance_vector
        )
        contact_damping_force = -contact_nu * normal_interpenetration_velocity

        # magnitude* direction
        net_contact_force = 0.5 * mask * (contact_damping_force + contact_force)

        # Compute friction
        slip_interpenetration_velocity = (
            interpenetration_velocity - normal_interpenetration_velocity
        )
        slip_interpenetration_velocity_mag = np.linalg.norm(
            slip_interpenetration_velocity
        )
        slip_interpenetration_velocity_unitized = slip_interpenetration_velocity / (
            slip_interpenetration_velocity_mag + 1e-14
        )
        # Compute friction force in the slip direction.
        damping_force_in_slip_direction = (
            velocity_damping_coefficient * slip_interpenetration_velocity_mag
        )
        # Compute Coulombic friction
        coulombic_friction_force = friction_coefficient * np.linalg.norm(
            net_contact_force
        )
        # Compare damping force in slip direction and kinetic friction and minimum is the friction force.
        friction_force = (
            -min(damping_force_in_slip_direction, coulombic_friction_force)
            * slip_interpenetration_velocity_unitized
        )
        # Update contact force
        net_contact_force += friction_force

        # Torques acting on the cylinder
        moment_arm = x_sphere_contact_point - x_sphere_center

        # Add it to the rods at the end of the day
        if i == 0:
            external_forces_rod[..., i] -= 2 / 3 * net_contact_force
            external_forces_rod[..., i + 1] -= 4 / 3 * net_contact_force
            sphere_total_contact_forces += 2.0 * net_contact_force
            sphere_total_contact_torques += np.cross(
                moment_arm, 2.0 * net_contact_force
            )
        elif i == n_points - 1:
            external_forces_rod[..., i] -= 4 / 3 * net_contact_force
            external_forces_rod[..., i + 1] -= 2 / 3 * net_contact_force
            sphere_total_contact_forces += 2.0 * net_contact_force
            sphere_total_contact_torques += np.cross(
                moment_arm, 2.0 * net_contact_force
            )
        else:
            external_forces_rod[..., i] -= net_contact_force
            external_forces_rod[..., i + 1] -= net_contact_force
            sphere_total_contact_forces += 2.0 * net_contact_force
            sphere_total_contact_torques += np.cross(
                moment_arm, 2.0 * net_contact_force
            )

    # Update the cylinder external forces and torques
    external_forces_sphere[..., 0] += sphere_total_contact_forces
    external_torques_sphere[..., 0] += (
        sphere_director_collection @ sphere_total_contact_torques
    )


class RodRodContact(NoContact):
    """
    This class is for applying contact forces between rod-rod.

    Examples
    --------

    How to define contact between rod and rod.
    >>> simulator.detect_contact_between(first_rod, second_rod).using(
    ...    RodRodContact,
    ...    k=1e4,
    ...    nu=10,
    ... )

    """

    def __init__(self, k: float, nu: float):
        """
        Parameters
        ----------
        k : float
            Contact spring constant.
        nu : float
            Contact damping constant.
        """
        super(RodRodContact, self).__init__()
        self.k = k
        self.nu = nu

    def _check_systems_validity(
        self,
        system_one: SystemType,
        system_two: AllowedContactType,
    ) -> None:
        """
        This checks the contact order and type of a SystemType object and an AllowedContactType object.
        For the RodRodContact class both systems must be distinct rods.

        Parameters
        ----------
        system_one
            SystemType
        system_two
            AllowedContactType
        """
        if not issubclass(system_one.__class__, RodBase) or not issubclass(
            system_two.__class__, RodBase
        ):
            raise TypeError(
                "Systems provided to the contact class have incorrect order. \n"
                " First system is {0} and second system is {1}. \n"
                " Both systems must be distinct rods".format(
                    system_one.__class__, system_two.__class__
                )
            )
        if system_one == system_two:
            raise TypeError(
                "First rod is identical to second rod. \n"
                "Rods must be distinct for RodRodConact. \n"
                "If you want self contact, use RodSelfContact instead"
            )

    def apply_contact(self, system_one: RodType, system_two: RodType):
        # First, check for a global AABB bounding box, and see whether that
        # intersects

        if _prune_using_aabbs_rod_rod(
            system_one.position_collection,
            system_one.radius,
            system_one.lengths,
            system_two.position_collection,
            system_two.radius,
            system_two.lengths,
        ):
            return

        _calculate_contact_forces_rod_rod(
            system_one.position_collection[
                ..., :-1
            ],  # Discount last node, we want element start position
            system_one.radius,
            system_one.lengths,
            system_one.tangents,
            system_one.velocity_collection,
            system_one.internal_forces,
            system_one.external_forces,
            system_two.position_collection[
                ..., :-1
            ],  # Discount last node, we want element start position
            system_two.radius,
            system_two.lengths,
            system_two.tangents,
            system_two.velocity_collection,
            system_two.internal_forces,
            system_two.external_forces,
            self.k,
            self.nu,
        )


class RodCylinderContact(NoContact):
    """
    This class is for applying contact forces between rod-cylinder.
    If you are want to apply contact forces between rod and cylinder, first system is always rod and second system
    is always cylinder.
    In addition to the contact forces, user can define apply friction forces between rod and cylinder that
    are in contact. For details on friction model refer to this [1]_.

    Notes
    -----
    The `velocity_damping_coefficient` is set to a high value (e.g. 1e4) to minimize slip and simulate stiction
    (static friction), while friction_coefficient corresponds to the Coulombic friction coefficient.

    Examples
    --------
    How to define contact between rod and cylinder.

    >>> simulator.detect_contact_between(rod, cylinder).using(
    ...    RodCylinderContact,
    ...    k=1e4,
    ...    nu=10,
    ... )


    .. [1] Preclik T., Popa Constantin., Rude U., Regularizing a Time-Stepping Method for Rigid Multibody Dynamics, Multibody Dynamics 2011, ECCOMAS. URL: https://www10.cs.fau.de/publications/papers/2011/Preclik_Multibody_Ext_Abstr.pdf
    """

    def __init__(
        self,
        k: float,
        nu: float,
        velocity_damping_coefficient=0.0,
        friction_coefficient=0.0,
    ):
        """

        Parameters
        ----------
        k : float
            Contact spring constant.
        nu : float
            Contact damping constant.
        velocity_damping_coefficient : float
            Velocity damping coefficient between rigid-body and rod contact is used to apply friction force in the
            slip direction.
        friction_coefficient : float
            For Coulombic friction coefficient for rigid-body and rod contact.
        """
        super(RodCylinderContact, self).__init__()
        self.k = k
        self.nu = nu
        self.velocity_damping_coefficient = velocity_damping_coefficient
        self.friction_coefficient = friction_coefficient

    def _check_systems_validity(
        self,
        system_one: SystemType,
        system_two: AllowedContactType,
    ) -> None:
        """
        This checks the contact order and type of a SystemType object and an AllowedContactType object.
        For the RodCylinderContact class first_system should be a rod and second_system should be a cylinder.

        Parameters
        ----------
        system_one
            SystemType
        system_two
            AllowedContactType
        """
        if not issubclass(system_one.__class__, RodBase) or not issubclass(
            system_two.__class__, Cylinder
        ):
            raise TypeError(
                "Systems provided to the contact class have incorrect order/type. \n"
                " First system is {0} and second system is {1}. \n"
                " First system should be a rod, second should be a cylinder".format(
                    system_one.__class__, system_two.__class__
                )
            )

    def apply_contact(self, system_one: RodType, system_two: SystemType):
        # First, check for a global AABB bounding box, and see whether that
        # intersects
        if _prune_using_aabbs_rod_cylinder(
            system_one.position_collection,
            system_one.radius,
            system_one.lengths,
            system_two.position_collection,
            system_two.director_collection,
            system_two.radius[0],
            system_two.length[0],
        ):
            return

        x_cyl = (
            system_two.position_collection[..., 0]
            - 0.5 * system_two.length * system_two.director_collection[2, :, 0]
        )

        rod_element_position = 0.5 * (
            system_one.position_collection[..., 1:]
            + system_one.position_collection[..., :-1]
        )
        _calculate_contact_forces_rod_cylinder(
            rod_element_position,
            system_one.lengths * system_one.tangents,
            system_two.position_collection[..., 0],
            x_cyl,
            system_two.length * system_two.director_collection[2, :, 0],
            system_one.radius + system_two.radius,
            system_one.lengths + system_two.length,
            system_one.internal_forces,
            system_one.external_forces,
            system_two.external_forces,
            system_two.external_torques,
            system_two.director_collection[:, :, 0],
            system_one.velocity_collection,
            system_two.velocity_collection,
            self.k,
            self.nu,
            self.velocity_damping_coefficient,
            self.friction_coefficient,
        )


class RodSelfContact(NoContact):
    """
    This class is modeling self contact of rod.

    How to define contact rod self contact.
    >>> simulator.detect_contact_between(rod, rod).using(
    ...    RodSelfContact,
    ...    k=1e4,
    ...    nu=10,
    ... )

    """

    def __init__(self, k: float, nu: float):
        """

        Parameters
        ----------
        k : float
            Contact spring constant.
        nu : float
            Contact damping constant.
        """
        super(RodSelfContact, self).__init__()
        self.k = k
        self.nu = nu

    def _check_systems_validity(
        self,
        system_one: SystemType,
        system_two: AllowedContactType,
    ) -> None:
        """
        This checks the contact order and type of a SystemType object and an AllowedContactType object.
        For the RodSelfContact class first_system and second_system should be the same rod.

        Parameters
        ----------
        system_one
            SystemType
        system_two
            AllowedContactType
        """
        if (
            not issubclass(system_one.__class__, RodBase)
            or not issubclass(system_two.__class__, RodBase)
            or system_one != system_two
        ):
            raise TypeError(
                "Systems provided to the contact class have incorrect order/type. \n"
                " First system is {0} and second system is {1}. \n"
                " First system and second system should be the same rod \n"
                " If you want rod rod contact, use RodRodContact instead".format(
                    system_one.__class__, system_two.__class__
                )
            )

    def apply_contact(self, system_one: RodType, system_two: RodType) -> None:
        _calculate_contact_forces_self_rod(
            system_one.position_collection[
                ..., :-1
            ],  # Discount last node, we want element start position
            system_one.radius,
            system_one.lengths,
            system_one.tangents,
            system_one.velocity_collection,
            system_one.external_forces,
            self.k,
            self.nu,
        )


class RodSphereContact(NoContact):
    """
    This class is for applying contact forces between rod-sphere.
    First system is always rod and second system is always sphere.
    In addition to the contact forces, user can define apply friction forces between rod and sphere that
    are in contact. For details on friction model refer to this [1]_.
    Notes
    -----
    The `velocity_damping_coefficient` is set to a high value (e.g. 1e4) to minimize slip and simulate stiction
    (static friction), while friction_coefficient corresponds to the Coulombic friction coefficient.
    Examples
    --------
    How to define contact between rod and sphere.
    >>> simulator.detect_contact_between(rod, sphere).using(
    ...    RodSphereContact,
    ...    k=1e4,
    ...    nu=10,
    ... )
    .. [1] Preclik T., Popa Constantin., Rude U., Regularizing a Time-Stepping Method for Rigid Multibody Dynamics, Multibody Dynamics 2011, ECCOMAS. URL: https://www10.cs.fau.de/publications/papers/2011/Preclik_Multibody_Ext_Abstr.pdf
    """

    def __init__(
        self,
        k: float,
        nu: float,
        velocity_damping_coefficient=0.0,
        friction_coefficient=0.0,
    ):
        """
        Parameters
        ----------
        k : float
            Contact spring constant.
        nu : float
            Contact damping constant.
        velocity_damping_coefficient : float
            Velocity damping coefficient between rigid-body and rod contact is used to apply friction force in the
            slip direction.
        friction_coefficient : float
            For Coulombic friction coefficient for rigid-body and rod contact.
        """
        super(RodSphereContact, self).__init__()
        self.k = k
        self.nu = nu
        self.velocity_damping_coefficient = velocity_damping_coefficient
        self.friction_coefficient = friction_coefficient

    def _check_systems_validity(
        self,
        system_one: SystemType,
        system_two: AllowedContactType,
    ) -> None:
        """
        This checks the contact order and type of a SystemType object and an AllowedContactType object.
        For the RodSphereContact class first_system should be a rod and second_system should be a sphere.
        Parameters
        ----------
        system_one
            SystemType
        system_two
            AllowedContactType
        """
        if not issubclass(system_one.__class__, RodBase) or not issubclass(
            system_two.__class__, Sphere
        ):
            raise TypeError(
                "Systems provided to the contact class have incorrect order/type. \n"
                " First system is {0} and second system is {1}. \n"
                " First system should be a rod, second should be a sphere".format(
                    system_one.__class__, system_two.__class__
                )
            )

    def apply_contact(self, system_one: RodType, system_two: SystemType):
        # First, check for a global AABB bounding box, and see whether that
        # intersects
        if _prune_using_aabbs_rod_sphere(
            system_one.position_collection,
            system_one.radius,
            system_one.lengths,
            system_two.position_collection,
            system_two.director_collection,
            system_two.radius[0],
        ):
            return

        x_sph = (
            system_two.position_collection[..., 0]
            - system_two.radius * system_two.director_collection[2, :, 0]
        )

        rod_element_position = 0.5 * (
            system_one.position_collection[..., 1:]
            + system_one.position_collection[..., :-1]
        )
        _calculate_contact_forces_rod_sphere(
            rod_element_position,
            system_one.lengths * system_one.tangents,
            system_two.position_collection[..., 0],
            x_sph,
            system_two.radius * system_two.director_collection[2, :, 0],
            system_one.radius + system_two.radius,
            system_one.lengths + 2 * system_two.radius,
            system_one.internal_forces,
            system_one.external_forces,
            system_two.external_forces,
            system_two.external_torques,
            system_two.director_collection[:, :, 0],
            system_one.velocity_collection,
            system_two.velocity_collection,
            self.k,
            self.nu,
            self.velocity_damping_coefficient,
            self.friction_coefficient,
        )
