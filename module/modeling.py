"""
Modeling functions for creating windings and PCB in AEDT designs.
"""

import math
import copy


def create_winding(design, name, up=True, *args, **kwargs):
    """
    Create a winding (coil) in the design.
    
    Args:
        design: pyDesign object
        name: Name of the winding
        up: Boolean indicating if winding is on top (True) or bottom (False)
        **kwargs: Additional parameters for winding creation
        
    Returns:
        tuple: (winding, port, coil_width)
    """
    color = kwargs.get("color", None)

    turns = kwargs.get("turns", None)
    layer = kwargs.get("layer", None)

    outer_x = kwargs.get("outer_x", None)
    outer_y = kwargs.get("outer_y", None)
    fillet = kwargs.get("fillet", None)
    inner = kwargs.get("inner", None)
    fill_factor = kwargs.get("fill_factor", None)
    theta1 = kwargs.get("theta1", None)
    theta2 = kwargs.get("theta2", None)

    PCB_thickness = kwargs.get("PCB_thickness", None)
    coil_gap = kwargs.get("coil_gap", None)  # layer 2개 이상일 경우 1층과 2층 사이의 거리
    move = kwargs.get("move", None)

    mul = 1

    if up == True:
        mul = 1
    else:
        mul = -1

    coil_width = None

    if layer == 1:

        winding_variables = {"outer_x": outer_x, "outer_y": outer_y, "fillet": fillet, "inner": inner, "fill_factor": fill_factor, "theta1": theta1, "theta2": theta2}
        coil = design.model3d.winding.coil_points(turns=turns, **winding_variables)
        coil.points.insert(0, [f"0mm", f"{coil.points[0][1]}", coil.points[0][2]])
        coil_width = coil.width

        winding = design.model3d.winding.create_polyline(name=name, points=coil.points, width=coil.width, height=PCB_thickness)
        winding.move([0,0,f"({move})"])

        if up:
            point1 = [f"{coil.points[0][0]}+{coil.width}/2", f"{coil.points[0][1]}-{coil.width}", f"({coil.points[0][2]} - ({PCB_thickness})/2)"]
            point2 = [f"{coil.points[0][0]}+{coil.width}/2", f"{coil.points[0][1]}-{coil.width}", f"({coil.points[0][2]} + (({coil_gap})+({PCB_thickness}))/2)"]
            ter1 = design.model3d.winding.create_polyline(name=f"{name}_ter1", points=[point1, point2], width=coil.width, height=coil.width)
            point1 = [f"{coil.points[-1][0]}-{coil.width}/2", f"{coil.points[-1][1]}+{coil.width}", f"({coil.points[-1][2]} - ({PCB_thickness})/2)"]
            point2 = [f"{coil.points[-1][0]}-{coil.width}/2", f"{coil.points[-1][1]}+{coil.width}", f"({coil.points[-1][2]} + (({coil_gap})+({PCB_thickness}))/2)"]
            ter2 = design.model3d.winding.create_polyline(name=f"{name}_ter2", points=[point1, point2], width=coil.width, height=coil.width)

            ter_edge1 = ter1.top_face_y.top_edge_z
            ter_edge2 = ter2.bottom_face_y.top_edge_z

        else:
            point1 = [f"{coil.points[0][0]}+{coil.width}/2", f"{coil.points[0][1]}-{coil.width}", f"({coil.points[0][2]} + ({PCB_thickness})/2)"]
            point2 = [f"{coil.points[0][0]}+{coil.width}/2", f"{coil.points[0][1]}-{coil.width}", f"({coil.points[0][2]} - ({coil_gap}) + 3*({PCB_thickness})/2)mm"]
            ter1 = design.model3d.winding.create_polyline(name=f"{name}_ter1", points=[point1, point2], width=coil.width, height=coil.width)
            point1 = [f"{coil.points[-1][0]}-{coil.width}/2", f"{coil.points[-1][1]}+{coil.width}", f"({coil.points[-1][2]} + ({PCB_thickness})/2)"]
            point2 = [f"{coil.points[-1][0]}-{coil.width}/2", f"{coil.points[-1][1]}+{coil.width}", f"({coil.points[-1][2]} - ({coil_gap}) + 3*({PCB_thickness})/2)mm"]
            ter2 = design.model3d.winding.create_polyline(name=f"{name}_ter2", points=[point1, point2], width=coil.width, height=coil.width)

            ter_edge1 = ter1.bottom_face_y.bottom_edge_z
            ter_edge2 = ter2.top_face_y.bottom_edge_z


    elif layer == 2:

        if turns % 2 == 1:
            turns = math.ceil(int(turns)/2)
            turns_sub = 1
        else:
            turns = math.ceil(int(turns)/2)
            turns_sub = 0

        # winding1
        winding_variables = {"outer_x": outer_x, "outer_y": outer_y, "fillet": fillet, "inner": inner, "fill_factor": fill_factor, "theta1": theta1, "theta2": theta2, "turns_sub": turns_sub}
        coil1 = design.model3d.winding.coil_points(turns=turns, **winding_variables)
        coil1.points.insert(0, [f"{coil1.points[0][0]}", f"{coil1.points[0][1]} - 1mm", coil1.points[0][2]])
        coil_width = coil1.width

        winding1 = design.model3d.winding.create_polyline(name=f"{name}_1", points=coil1.points, width=coil1.width, height=PCB_thickness)
        winding1.move([0,0,f"({move})"])

        # winding2
        winding_variables = {"outer_x": outer_x, "outer_y": outer_y, "fillet": fillet, "inner": inner, "fill_factor": fill_factor, "theta1": theta1, "theta2": theta2, "turns_sub": 0}
        coil2 = design.model3d.winding.coil_points(turns=turns, **winding_variables)
        coil2.points.insert(0, [f"{coil2.points[0][0]}", f"{coil2.points[0][1]} - 1mm", coil2.points[0][2]])

        # 끝단 길이 맞춰줌
        coil2.points[0][1] = coil1.points[0][1]

        coil2.points.insert(2, copy.deepcopy(coil2.points[1]))
        coil2.points[0][0] = f"-({coil1.points[0][0]}) + 1.2*(({coil1.width}) + ({coil2.width}))/2"
        coil2.points[1][0] = f"-({coil1.points[0][0]}) + 1.2*(({coil1.width}) + ({coil2.width}))/2"

        winding2 = design.model3d.winding.create_polyline(name=f"{name}_2", points=coil2.points, width=coil2.width, height=PCB_thickness)
        winding2.move([0,0,f"({move}) + ({mul})*(({PCB_thickness}) + ({coil_gap}))"])
        winding2.mirror(origin=[0,0,0], vector=[1,0,0])

        via_height = f"(2*({PCB_thickness})+({coil_gap}))"
        via = design.model3d.winding.create_via(name=f"{name}_via", center=coil2.points[-1], outer_R=f"0.8*{coil2.width}/2", inner_R=f"0.6*{coil2.width}/2", height=via_height, via_pad_R=f"1.2*{coil2.width}/2", via_pad_thick=PCB_thickness)
        via.move([0,0,f"({move}) + ({mul})*(({PCB_thickness}) + ({coil_gap}))/2"])

        # winding = design.model3d.unite(assignment=[winding1, winding2, via])
        

        point1 = coil1.points[0]
        point2 = [point1[0], f"({point1[1]})-0.05mm", point1[2]]
        ter1 = design.model3d.winding.create_polyline(name=f"{name}_ter1", points=[point1, point2], width=coil1.width, height=PCB_thickness)
        point1 = coil2.points[0]
        point2 = [point1[0], f"({point1[1]})-0.05mm", point1[2]]
        ter2 = design.model3d.winding.create_polyline(name=f"{name}_ter2", points=[point1, point2], width=coil2.width, height=PCB_thickness)
        ter2.move([0,0,f"({mul})*(({PCB_thickness}) + ({coil_gap}))"])
        ter2.mirror(origin=[0,0,0], vector=[1,0,0])

        if up == True:
            ter_edge1 = ter1.bottom_face_x.top_edge_z
            ter_edge2 = ter2.top_face_x.bottom_edge_z
        else:
            ter_edge1 = ter1.bottom_face_x.bottom_edge_z
            ter_edge2 = ter2.top_face_x.top_edge_z

        # return [winding1, winding2], ter1, ter2, ter_face, coil_width
        return [winding1, winding2]

    ter1.material_name = "pec"
    ter2.material_name = "pec"

    ter1.move([0,0,f"({move})"])
    ter2.move([0,0,f"({move})"])

    ter_edges = design.modeler.create_object_from_edge([ter_edge1, ter_edge2], non_model=False)
    ter_face = design.modeler.connect(ter_edges) # list object
    ter_face = ter_face[0]

    winding.color = color
    winding.transparency = 0
    winding.material_name = "copper"


    # return winding, ter1, ter2, ter_face, coil_width
    return winding


def create_port(design, name, ter_ref, ter_face) :

    port = design.lumped_port(assignment=ter_face, reference=[ter_ref], create_port_sheet=False, port_on_plane=True, integration_line=0, impedance=50, name=f"{name}_port", renormalize=True, deembed=False, terminals_rename=True)
    # assignment를 list의 형태로 주지 말것 (port 안생김)

    return port


def create_PCB(design):
    """
    Create a PCB in the design.
    
    Args:
        design: pyDesign object
        
    Returns:
        PCB object
    """
    if design.variables["Tx_outer_x"] > design.variables["Rx_outer_x"]:
        outer_x = "(Tx_outer_x)"
    else:
        outer_x = "(Rx_outer_x)"
    
    if design.variables["Tx_outer_y"] > design.variables["Rx_outer_y"]:
        outer_y = "(Tx_outer_y)"
    else:
        outer_y = "(Rx_outer_y)"

    origin_z = f"Tx_Rx_gap/2 + Rx_layer*PCB_thickness + (Rx_layer-1)*Rx_Rx_gap"
    dimension_z = f"({origin_z}) + Tx_Rx_gap/2 + Tx_layer*PCB_thickness + (Tx_layer-1)*Tx_Tx_gap"

    ratio = 1.6

    origin = [f"-({ratio}*{outer_x})", f"-({ratio}*{outer_y})", f"-({origin_z})"]
    dimension = [f"2*({ratio}*{outer_x})", f"2*({ratio}*{outer_y})", f"({dimension_z})"]

    PCB = design.modeler.create_box(
        origin=origin,
        sizes=dimension,
        name="PCB",
        material="FR4_epoxy"
    )

    return PCB



def create_region(design):

    region = design.modeler.create_air_region(x_pos="175",x_neg="175", y_pos="175",y_neg="175", z_pos = "2500", z_neg="2500")
    design.assign_material(assignment=region, material="vacuum")
    design.assign_radiation_boundary_to_objects(assignment=region, name="Radiation")

    return region