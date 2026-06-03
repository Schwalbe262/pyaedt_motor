"""IPMSM geometry builder used by the batch runner.

This module contains the modeling portion that was validated in
``pyaedt_test.ipynb``.  Keeping it in a normal Python module avoids notebook
state/cache issues when running many AEDT simulations in parallel.
"""

from __future__ import annotations

from typing import Any
import math


def create_ipmsm_design(project: Any, sim: Any, design_name: str = "IPMSM") -> tuple[Any, Any, dict[str, list[str]]]:
    """Create the IPMSM Maxwell 2D design and return object groups for setup."""
    design1 = project.create_design(name=design_name, solver="Maxwell 2D", solution="Transient")

    sim.project = project

    input_data = sim.set_variable(design1)


    # stator modeling

    stator = design1.modeler.create_circle(origin=[0, 0, 0], radius="stator_outer_radius", num_sides=72, name="stator", material="copper")
    stator_sub = design1.modeler.create_circle(origin=[0, 0, 0], radius="stator_outer_radius - stator_back_yoke_thick", num_sides=72, name="stator_sub", material="copper")
    design1.modeler.subtract(blank_list=stator, tool_list=[stator_sub], keep_originals=False)

    teeth = design1.modeler.create_rectangle(origin=["stator_outer_radius - stator_back_yoke_thick", "-stator_teeth_width/2", 0], sizes=["-stator_teeth_length", "stator_teeth_width"], name="stator_teeth", material="copper")
    design1.modeler.duplicate_around_axis(assignment=[teeth], axis="Z", angle="(360/slot_num)deg", clones="slot_num", create_new_objects=False)

    stator_gap = design1.modeler.create_circle(origin=[0, 0, 0], radius="stator_inner_radius + stator_gap", num_sides=72, name="stator_gap", material="copper")
    stator_gap_sub = design1.modeler.create_circle(origin=[0, 0, 0], radius="stator_inner_radius", num_sides=72, name="stator_gap_sub", material="copper")
    design1.modeler.subtract(blank_list=stator_gap, tool_list=[stator_gap_sub], keep_originals=False)

    slot_opening = design1.modeler.create_rectangle(origin=[0, "-slot_opening/2", 0], sizes=["stator_outer_radius", "slot_opening"], name="slot_opening", material="copper")
    design1.modeler.rotate(assignment=[slot_opening], axis="Z", angle="(360/slot_num/2)deg")
    design1.modeler.duplicate_around_axis(assignment=[slot_opening], axis="Z", angle="(360/slot_num)deg", clones="slot_num", create_new_objects=False)
    design1.modeler.subtract(blank_list=stator_gap, tool_list=[slot_opening], keep_originals=False)


    stator_gaps = design1.modeler.separate_bodies(assignment=[stator_gap.name])

    def max_x_of_top_edge(obj):
        e = obj.top_edge_x
        return e.midpoint[0]

    best_obj = max(stator_gaps, key=max_x_of_top_edge)

    edge = best_obj.top_edge_y

    def x_of_vertex(v):
        p = v.position
        return float("-inf") if p is None else p[0]

    v_max = max(edge.vertices, key=x_of_vertex)
    pos = v_max.position
    x, y, z = pos[0], pos[1], pos[2]

    stator_gap_filler = design1.modeler.create_polyline(
        points=[
            (x,  y, z),
            ("stator_outer_radius - stator_back_yoke_thick - stator_teeth_length",  sim.stator_teeth_width / 2,  0),
            ("stator_outer_radius - stator_back_yoke_thick - stator_teeth_length", -sim.stator_teeth_width / 2,  0),
            (x, -y, z),
            (x,  y, z)
        ],
        cover_surface=True,
        name="stator_gap_filler",
        material="copper"
    )

    stator_gap_sub_temp = design1.modeler.create_circle(origin=[0, 0, 0], radius="stator_inner_radius", num_sides=72, name="stator_gap_sub_filler", material="copper")
    design1.modeler.subtract(blank_list=stator_gap_filler, tool_list=[stator_gap_sub_temp], keep_originals=False)
    design1.modeler.duplicate_around_axis(assignment=[stator_gap_filler], axis="Z", angle="(360/slot_num)deg", clones="slot_num", create_new_objects=False)

    objects_list = [stator, *stator_gaps, stator_gap_filler, teeth]
    main_stator = design1.modeler.unite(assignment=objects_list)
    main_stator = design1.modeler.get_object_from_name(main_stator)
    main_stator.color = [192, 192, 192]


    # winding modeling

    x1 = "(stator_outer_radius - stator_back_yoke_thick - stator_teeth_length)"
    x2 = "{(stator_outer_radius - stator_back_yoke_thick)}"

    theta = "atan((stator_outer_radius - stator_back_yoke_thick)/(stator_teeth_width/2))"
    x2 = f"(stator_outer_radius - stator_back_yoke_thick) * cos({theta}) * cos({theta})"
    x2 = f"sqrt((stator_outer_radius - stator_back_yoke_thick)^2 - (stator_teeth_width/2)^2)"
    # x2 = f"(stator_outer_radius - stator_back_yoke_thick)"



    y1 = "stator_teeth_width/2"
    y2 = "(-(stator_teeth_width/2))"

    # y1 = f"{x1} * cos({theta})"
    # y2 = f"-({x1} * cos({theta}))"


    winding = design1.modeler.create_polyline(
        points=[
            (x1, y1,  0),
            (x2, y1,  0),
            (f"{x2}*cos((360/slot_num)*pi/180) - {y2}*sin((360/slot_num)*pi/180)", f"{x2}*sin((360/slot_num)*pi/180) + {y2}*cos((360/slot_num)*pi/180)",  0),
            (f"{x1}*cos((360/slot_num)*pi/180) - {y2}*sin((360/slot_num)*pi/180)", f"{x1}*sin((360/slot_num)*pi/180) + {y2}*cos((360/slot_num)*pi/180)",  0),
            (x1, y1,  0)
        ],
        cover_surface=True,
        name="winding",
        material="copper"
    )

    design1.modeler.rotate(assignment=[winding], axis="Z", angle="-(360/slot_num/2)deg")
    windings = design1.modeler.split(assignment=[winding], plane="XZ")
    design1.modeler.rotate(assignment=windings, axis="Z", angle="(360/slot_num/2)deg")
    design1.modeler.duplicate_around_axis(assignment=windings, axis="Z", angle="(360/slot_num)deg", clones="slot_num", create_new_objects=False)
    windings = design1.modeler.separate_bodies(assignment=windings) + [design1.modeler.get_object_from_name(w) for w in windings]

    def winding_center_xy(w):
        """Return the approximate center of a winding sheet in XY."""
        f0 = w.faces[0]
        if not isinstance(f0, int):
            c = f0.center
            return float(c[0]), float(c[1])
        bb = w.bounding_box
        if isinstance(bb, (list, tuple)) and len(bb) >= 6:
            return (bb[0] + bb[3]) / 2.0, (bb[1] + bb[4]) / 2.0
        if isinstance(bb, (list, tuple)) and len(bb) >= 4:
            return (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
        raise TypeError("Unexpected bounding_box format", type(bb), bb)


    def angle_ccw_from_pos_x(xy):
        x, y = xy
        ang = math.atan2(y, x)
        if ang < 0:
            ang += 2 * math.pi
        return ang


    sorted_windings = sorted(
        windings,
        key=lambda w: angle_ccw_from_pos_x(winding_center_xy(w)),
    )

    for i, w in enumerate(sorted_windings):
        w.name = f"__w_tmp_{i}"

    for i, w in enumerate(sorted_windings, start=1):
        w.name = f"winding{i}"

    windings = sorted_windings


    # rotator modeling

    rotator = design1.modeler.create_circle(origin=[0, 0, 0], radius="rotor_radius", num_sides=72, name="rotator", material="copper")
    shaft = design1.modeler.create_circle(origin=[0, 0, 0], radius="shaft_radius", num_sides=72, name="shaft", material="copper")
    design1.modeler.subtract(blank_list=rotator, tool_list=[shaft], keep_originals=True)
    rotator.color = [156, 156, 156]
    shaft.color = [192, 192, 192]



    # magnet modeling
    barrier = design1.modeler.create_rectangle(origin=[0, "-magnet_shield_thick/2", 0], sizes=["stator_outer_radius", "magnet_shield_thick"], name="barriers", material="copper")
    design1.modeler.rotate(assignment=[barrier], axis="Z", angle="(360/pole_num/2)deg")
    design1.modeler.duplicate_around_axis(assignment=[barrier], axis="Z", angle="(360/pole_num)deg", clones="pole_num", create_new_objects=False)

    magnet_space = design1.modeler.create_rectangle(origin=["rotor_radius - magnet_setback", "-magnet_height*magnet_space_height_ratio/2", 0], sizes=["-magnet_thick", "magnet_height*magnet_space_height_ratio"], name="magnet_space", material="copper")
    design1.modeler.subtract(blank_list=magnet_space, tool_list=[barrier], keep_originals=True)
    magnet_space = design1.modeler.separate_bodies(assignment=magnet_space)
    def object_center_xy(obj):
        """Return the approximate center of a 2D object in XY."""
        f0 = obj.faces[0]
        if not isinstance(f0, int):
            c = f0.center
            return float(c[0]), float(c[1])
        bb = obj.bounding_box
        if isinstance(bb, (list, tuple)) and len(bb) >= 6:
            return (bb[0] + bb[3]) / 2.0, (bb[1] + bb[4]) / 2.0
        if isinstance(bb, (list, tuple)) and len(bb) >= 4:
            return (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
        raise TypeError("Unexpected bounding_box format", type(bb), bb)
    best = min(magnet_space, key=lambda o: abs(object_center_xy(o)[1]))
    others = [o for o in magnet_space if o.name != best.name]
    if others:
        design1.modeler.delete(assignment=others)
    magnet_space = design1.modeler.get_object_from_name(best.name)

    temp = design1.modeler.create_circle(origin=[0, 0, 0], radius="stator_outer_radius", num_sides=72, name="temp", material="air")
    temp_sub = design1.modeler.create_circle(origin=[0, 0, 0], radius="rotor_radius - magnet_shield_thick", num_sides=72, name="temp_sub", material="air")
    design1.modeler.subtract(blank_list=temp, tool_list=[temp_sub], keep_originals=False)
    design1.modeler.subtract(blank_list=magnet_space, tool_list=[temp], keep_originals=False)




    edge = magnet_space.top_edge_x

    assert len(edge.vertices) >= 2
    p0 = edge.vertices[0].position
    p1 = edge.vertices[1].position

    x0, y0, z0 = p0[0], p0[1], p0[2]
    x1, y1, z1 = p1[0], p1[1], p1[2]

    magnet_height = abs(y0)

    same_pole_count = max(1, int(round(float(sim.pole_num) / 2.0)))

    def sort_by_angle(objects):
        def angle_of(obj):
            cx, cy = object_center_xy(obj)
            return math.atan2(cy, cx) % (2 * math.pi)

        return sorted(objects, key=angle_of)

    def duplicate_magnets(seed_obj, prefix):
        result = design1.modeler.duplicate_around_axis(
            assignment=[seed_obj],
            axis="Z",
            angle="(360/pole_num*2)deg",
            clones=same_pole_count,
            create_new_objects=True,
        )
        added_names = result[1] if isinstance(result, tuple) and len(result) > 1 else []
        objects = [seed_obj]
        for name in added_names:
            obj = design1.modeler.get_object_from_name(name)
            if obj:
                objects.append(obj)
        objects = sort_by_angle(objects)
        if len(objects) > same_pole_count:
            extras = objects[same_pole_count:]
            design1.modeler.delete(assignment=extras)
            objects = objects[:same_pole_count]
        renamed = []
        for index, obj in enumerate(objects, start=1):
            obj.name = f"{prefix}_{index:02d}"
            renamed.append(obj)
        return renamed

    magnet_N_seed = design1.modeler.create_rectangle(
        origin=["rotor_radius - magnet_setback", f"-{magnet_height}mm*magnet_height_ratio", 0], 
        sizes=["-magnet_thick", f"{magnet_height}mm*magnet_height_ratio*2"], 
        name="magnet_N_seed", 
        material="iron")
    magnet_N_seed.color = [255, 0, 0]
    magnet_N_seed.transparency = 0
    design1.modeler.delete(assignment=barrier)


    design1.modeler.duplicate_around_axis(assignment=[magnet_space], axis="Z", angle="(360/pole_num)deg", clones="pole_num", create_new_objects=False)
    n_magnets = duplicate_magnets(magnet_N_seed, "magnet_N")


    design1.modeler.copy(assignment=n_magnets[0])
    magnet_S_seed_names = design1.modeler.paste()
    magnet_S_seed = design1.modeler.get_object_from_name(magnet_S_seed_names[0])
    magnet_S_seed.name = "magnet_S_seed"
    magnet_S_seed.color = [0, 0, 255]
    magnet_S_seed.transparency = 0
    design1.modeler.rotate(assignment=magnet_S_seed, axis="Z", angle="(360/pole_num)deg")
    s_magnets = duplicate_magnets(magnet_S_seed, "magnet_S")


    design1.modeler.subtract(blank_list=rotator, tool_list=[magnet_space], keep_originals=True)
    all_magnets = n_magnets + s_magnets
    design1.modeler.subtract(blank_list=magnet_space, tool_list=all_magnets, keep_originals=True)

    object_groups = {
        "stator": [main_stator.name],
        "rotor": [rotator.name],
        "shaft": [shaft.name],
        "magnets": [magnet.name for magnet in all_magnets],
        "windings": [w.name for w in windings],
        "region": ["Region"],
        "band": ["Band"],
    }
    return design1, input_data, object_groups
