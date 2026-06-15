"""
Variable setting functions for simulation design.
"""

import math

try:
    import pandas as pd
except ModuleNotFoundError:
    pd = None


class _SimpleRow:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _SimpleIloc:
    def __init__(self, rows):
        self._rows = rows

    def __getitem__(self, index):
        return _SimpleRow(self._rows[index])


class SimpleInputFrame:
    def __init__(self, raw, columns):
        self._rows = [dict(zip(columns, raw))]
        self.iloc = _SimpleIloc(self._rows)

    def __len__(self):
        return len(self._rows)


GEOMETRY_INPUT_COLUMNS = [
    'slot_num', 'pole_num', 'stator_outer_radius', 'stator_back_yoke_thick_ratio', 'stator_back_yoke_thick', 'stator_inner_ratio', 'stator_inner_radius',
    'stator_shoe_thick', 'stator_teeth_length_ratio', 'stator_teeth_width_ratio', 'stator_teeth_length', 'stator_teeth_width',
    'stator_gap', 'slot_opening_ratio',
    'rotator_gap',
    'shaft_ratio',
    'rotor_radius',
    'shaft_radius',
    'magnet_shield_thick',
    'magnet_setback_ratio',
    'magnet_thick_ratio',
    'magnet_space_height_ratio',
    'magnet_height_ratio',
]


def _set_design_value(design, name, value, unit=""):
    design[name] = f"{value}{unit}" if unit else f"{value}"


def _populate_derived_geometry(simulation, design):
    simulation.stator_back_yoke_thick = simulation.stator_outer_radius * simulation.stator_back_yoke_thick_ratio
    simulation.stator_inner_radius = simulation.stator_outer_radius * simulation.stator_inner_ratio
    simulation.stator_teeth_length = (simulation.stator_outer_radius - simulation.stator_back_yoke_thick - simulation.stator_inner_radius) * simulation.stator_teeth_length_ratio
    angle_rad = math.radians(360 / simulation.slot_num)
    simulation.stator_teeth_width = (simulation.stator_outer_radius - simulation.stator_back_yoke_thick - simulation.stator_teeth_length) * math.tan(angle_rad/2) * simulation.stator_teeth_width_ratio * 2
    simulation.slot_opening = (2*(simulation.stator_outer_radius - simulation.stator_back_yoke_thick - simulation.stator_teeth_length) * math.sin(angle_rad/2) - simulation.stator_teeth_width * math.cos(angle_rad/2)) * simulation.slot_opening_ratio
    simulation.pole_angle = 360 / simulation.pole_num
    simulation.rotor_radius = simulation.stator_inner_radius - simulation.rotator_gap
    simulation.shaft_radius = simulation.rotor_radius * simulation.shaft_ratio
    simulation.rotor_thick = simulation.rotor_radius - simulation.shaft_radius
    simulation.magnet_setback = simulation.rotor_thick * simulation.magnet_setback_ratio
    simulation.magnet_thick = simulation.rotor_thick * simulation.magnet_thick_ratio
    simulation.magnet_height = ((simulation.rotor_radius - simulation.magnet_setback - simulation.magnet_thick) * math.cos(math.pi/simulation.pole_num) - simulation.magnet_shield_thick)

    design["stator_back_yoke_thick"] = f"stator_outer_radius * stator_back_yoke_thick_ratio"
    design["stator_inner_radius"] = f"stator_outer_radius * stator_inner_ratio"
    design["stator_teeth_length"] = f"(stator_outer_radius - stator_back_yoke_thick - stator_inner_radius) * stator_teeth_length_ratio"
    design["stator_teeth_width"] = f"(stator_outer_radius - stator_back_yoke_thick - stator_teeth_length) * tan(360deg/slot_num/2) * stator_teeth_width_ratio * 2"
    design["slot_opening"] = f"(2*(stator_outer_radius-stator_back_yoke_thick-stator_teeth_length)*sin(360deg/slot_num/2) - stator_teeth_width*cos(360deg/slot_num/2)) * slot_opening_ratio"
    design["rotor_radius"] = f"stator_inner_radius - rotator_gap"
    design["shaft_radius"] = f"rotor_radius * shaft_ratio"
    design["rotor_thick"] = f"rotor_radius - shaft_radius"
    design["magnet_setback"] = f"(rotor_thick * magnet_setback_ratio)"
    design["magnet_thick"] = f"(rotor_thick * magnet_thick_ratio)"
    design["magnet_height"] = f"(((rotor_radius - magnet_setback - magnet_thick) * cos(360deg/pole_num/2)) - magnet_shield_thick)"


def _set_fixed_geometry(simulation, design, values):
    simulation.slot_num = int(values["slot_num"])
    simulation.pole_num = int(values["pole_num"])
    simulation.stator_outer_radius = float(values["stator_outer_radius"])
    simulation.stator_back_yoke_thick_ratio = float(values["stator_back_yoke_thick_ratio"])
    simulation.stator_inner_ratio = float(values["stator_inner_ratio"])
    simulation.stator_shoe_thick = float(values["stator_shoe_thick"])
    simulation.stator_teeth_length_ratio = float(values["stator_teeth_length_ratio"])
    simulation.stator_teeth_width_ratio = float(values["stator_teeth_width_ratio"])
    simulation.stator_gap = float(values["stator_gap"])
    simulation.slot_opening_ratio = float(values.get("slot_opening_ratio", 0.09))
    simulation.rotator_gap = float(values["rotator_gap"])
    simulation.shaft_ratio = float(values["shaft_ratio"])
    simulation.magnet_shield_thick = float(values["magnet_shield_thick"])
    simulation.magnet_setback_ratio = float(values["magnet_setback_ratio"])
    simulation.magnet_thick_ratio = float(values["magnet_thick_ratio"])
    simulation.magnet_space_height_ratio = float(values.get("magnet_space_height_ratio", 1.0))
    simulation.magnet_height_ratio = float(values["magnet_height_ratio"])

    _set_design_value(design, "slot_num", simulation.slot_num)
    _set_design_value(design, "pole_num", simulation.pole_num)
    _set_design_value(design, "stator_outer_radius", simulation.stator_outer_radius, "mm")
    _set_design_value(design, "stator_back_yoke_thick_ratio", simulation.stator_back_yoke_thick_ratio)
    _set_design_value(design, "stator_inner_ratio", simulation.stator_inner_ratio)
    _set_design_value(design, "stator_shoe_thick", simulation.stator_shoe_thick, "mm")
    _set_design_value(design, "stator_teeth_length_ratio", simulation.stator_teeth_length_ratio)
    _set_design_value(design, "stator_teeth_width_ratio", simulation.stator_teeth_width_ratio)
    _set_design_value(design, "stator_gap", simulation.stator_gap, "mm")
    _set_design_value(design, "slot_opening_ratio", simulation.slot_opening_ratio)
    _set_design_value(design, "rotator_gap", simulation.rotator_gap, "mm")
    _set_design_value(design, "shaft_ratio", simulation.shaft_ratio)
    _set_design_value(design, "magnet_shield_thick", simulation.magnet_shield_thick, "mm")
    _set_design_value(design, "magnet_setback_ratio", simulation.magnet_setback_ratio)
    _set_design_value(design, "magnet_thick_ratio", simulation.magnet_thick_ratio)
    _set_design_value(design, "magnet_space_height_ratio", simulation.magnet_space_height_ratio)
    _set_design_value(design, "magnet_height_ratio", simulation.magnet_height_ratio)
    _populate_derived_geometry(simulation, design)


def _geometry_input_dataframe(simulation):
    simulation.input_raw = [
        simulation.slot_num, simulation.pole_num, simulation.stator_outer_radius, simulation.stator_back_yoke_thick_ratio, simulation.stator_back_yoke_thick, simulation.stator_inner_ratio, simulation.stator_inner_radius,
        simulation.stator_shoe_thick, simulation.stator_teeth_length_ratio, simulation.stator_teeth_width_ratio, simulation.stator_teeth_length, simulation.stator_teeth_width, simulation.stator_gap, simulation.slot_opening_ratio,
        simulation.rotator_gap, simulation.shaft_ratio, simulation.rotor_radius, simulation.shaft_radius, simulation.magnet_shield_thick, simulation.magnet_setback_ratio, simulation.magnet_thick_ratio,
        simulation.magnet_space_height_ratio, simulation.magnet_height_ratio,
    ]
    if pd is not None:
        simulation.input = pd.DataFrame([simulation.input_raw], columns=GEOMETRY_INPUT_COLUMNS)
    else:
        simulation.input = SimpleInputFrame(simulation.input_raw, GEOMETRY_INPUT_COLUMNS)
    return simulation.input


def set_variable(simulation, design, debug=False, input_list=None):
    """
    Set random variables for the simulation design.
    
    Args:
        simulation: Simulation object to store variable values
        design: pyDesign object to set design variables
    """
    # # Tx variables
    # simulation.Tx_outer_x = design.random_variable(variable_name="Tx_outer_x", lower=0.8, upper=2.5, resolution=0.1, unit="mm")
    # simulation.Tx_ratio = design.random_variable(lower=0.4, upper=0.9, resolution=0.01)
    # simulation.Tx_outer_y = simulation.Tx_ratio * simulation.Tx_outer_x

    # Tx_inner_min = 0.4 * min(simulation.Tx_outer_x, simulation.Tx_outer_y)
    # Tx_inner_max = 0.8 * min(simulation.Tx_outer_x, simulation.Tx_outer_y)
    # simulation.Tx_inner = design.random_variable(variable_name="Tx_inner", lower=Tx_inner_min, upper=Tx_inner_max, resolution=0.01, unit="mm")

    # Tx_fillet_min = simulation.Tx_inner * (math.tan(75 * math.pi/180) - 1) / math.tan(75 * math.pi/180) * 1.1
    # Tx_fillet_max = simulation.Tx_inner * 0.8 if simulation.Tx_inner * 0.8 > Tx_fillet_min else simulation.Tx_inner * 0.9
    # simulation.Tx_fillet = design.random_variable(variable_name="Tx_fillet", lower=Tx_fillet_min, upper=Tx_fillet_max, resolution=0.01, unit="mm")

    # simulation.Tx_fill_factor = design.random_variable(variable_name="Tx_fill_factor", lower=0.3, upper=0.8, resolution=0.01, unit="")

    # # Rx variables
    # simulation.Tx_Rx_ratio = design.random_variable(lower=0.6, upper=1.5, resolution=0.01)  # x방향 기준으로 Rx가 Tx보다 몇배 더 큰지 설정
    # simulation.Rx_outer_x = simulation.Tx_outer_x * simulation.Tx_Rx_ratio

    # simulation.Rx_ratio = design.random_variable(lower=0.4, upper=0.9, resolution=0.01)
    # simulation.Rx_outer_y = simulation.Rx_ratio * simulation.Rx_outer_x

    """
    임시변경
    """

    if debug:

        [simulation.Radius] = input_list

        design["Radius"] = f"{simulation.Radius}mm"

        design["Radius"] = f"{simulation.Radius}mm"

         # Create input DataFrame
        columns = [
            'Radius',
        ]

        simulation.input_raw = [
            simulation.Tx_turns, simulation.Rx_turns, simulation.Tx_layer, simulation.Rx_layer, simulation.Tx_outer_x, simulation.Tx_outer_y, simulation.Tx_ratio,
            simulation.Rx_outer_x, simulation.Rx_outer_y, simulation.Rx_ratio, simulation.Tx_inner, simulation.Rx_inner, simulation.Tx_fillet, simulation.Rx_fillet,
            simulation.Tx_fill_factor, simulation.Rx_fill_factor, simulation.PCB_thickness, simulation.Tx_Tx_gap, simulation.Rx_Rx_gap, simulation.Tx_Rx_gap
        ]
        
        simulation.input = pd.DataFrame([simulation.input_raw], columns=columns)

        return simulation.input



    # x 사이즈 제한 5mm
    # y 사이즈 제한 3.5mm

    fixed_geometry = getattr(simulation, "fixed_geometry", None)
    if fixed_geometry:
        _set_fixed_geometry(simulation, design, fixed_geometry)
        return _geometry_input_dataframe(simulation)

    simulation.stator_outer_radius = design.random_variable(variable_name="stator_outer_radius", lower=120, upper=200, resolution=0.1, unit="mm")
    simulation.stator_back_yoke_thick_ratio = design.random_variable(variable_name="stator_back_yoke_thick_ratio", lower=0.1, upper=0.15, resolution=0.001)
    simulation.stator_inner_ratio = design.random_variable(variable_name="stator_inner_ratio", lower=0.4, upper=0.6, resolution=0.001)
    simulation.stator_shoe_thick = design.random_variable(variable_name="stator_shoe_thick", lower=1, upper=2, resolution=0.1, unit="mm")

    # slot number
    simulation.slot_num = design.random_variable(variable_name="slot_num", lower=12, upper=12, resolution=2) # 8~16
    # stator teeth width
    simulation.stator_teeth_length_ratio = design.random_variable(variable_name="stator_teeth_length_ratio", lower=0.80, upper=0.90, resolution=0.001)
    simulation.stator_teeth_width_ratio = design.random_variable(variable_name="stator_teeth_width_ratio", lower=0.4, upper=0.8, resolution=0.001)
    simulation.stator_gap = design.random_variable(variable_name="stator_gap", lower=1, upper=3, resolution=0.01, unit="mm")

    simulation.stator_back_yoke_thick = simulation.stator_outer_radius * simulation.stator_back_yoke_thick_ratio
    simulation.stator_inner_radius = simulation.stator_outer_radius * simulation.stator_inner_ratio
    simulation.stator_teeth_length = (simulation.stator_outer_radius - simulation.stator_back_yoke_thick - simulation.stator_inner_radius) * simulation.stator_teeth_length_ratio
    angle_rad = math.radians(360 / simulation.slot_num)
    simulation.stator_teeth_width = (simulation.stator_outer_radius - simulation.stator_back_yoke_thick - simulation.stator_teeth_length) * math.tan(angle_rad/2) * simulation.stator_teeth_width_ratio * 2

    
                         
    design["stator_back_yoke_thick"] = f"stator_outer_radius * stator_back_yoke_thick_ratio"
    design["stator_inner_radius"] = f"stator_outer_radius * stator_inner_ratio"
    design["stator_teeth_length"] = f"(stator_outer_radius - stator_back_yoke_thick - stator_inner_radius) * stator_teeth_length_ratio"
    design["stator_teeth_width"] = f"(stator_outer_radius - stator_back_yoke_thick - stator_teeth_length) * tan(360deg/slot_num/2) * stator_teeth_width_ratio * 2"

    simulation.slot_opening_ratio = design.random_variable(variable_name="slot_opening_ratio", lower=0.03, upper=0.15, resolution=0.001)
    simulation.slot_opening = (2*(simulation.stator_outer_radius - simulation.stator_back_yoke_thick - simulation.stator_teeth_length) * math.sin(angle_rad/2) - simulation.stator_teeth_width * math.cos(angle_rad/2)) * simulation.slot_opening_ratio

    design["slot_opening"] = f"(2*(stator_outer_radius-stator_back_yoke_thick-stator_teeth_length)*sin(360deg/slot_num/2) - stator_teeth_width*cos(360deg/slot_num/2)) * slot_opening_ratio"

    
    # ploe number
    simulation.pole_num = design.random_variable(variable_name="pole_num", lower=8, upper=8, resolution=2) # 4~8
    simulation.pole_angle = 360 / simulation.pole_num

    simulation.rotator_gap = design.random_variable(variable_name="rotator_gap", lower=1, upper=3, resolution=0.01, unit="mm")
    simulation.shaft_ratio = design.random_variable(variable_name="shaft_ratio", lower=0.4, upper=0.6, resolution=0.001)
    simulation.rotor_radius = simulation.stator_inner_radius - simulation.rotator_gap
    simulation.shaft_radius = simulation.rotor_radius * simulation.shaft_ratio
    simulation.rotor_thick = simulation.rotor_radius - simulation.shaft_radius

    simulation.magnet_shield_thick = design.random_variable(variable_name="magnet_shield_thick", lower=1, upper=5, resolution=0.001, unit="mm")
    simulation.magnet_setback_ratio = design.random_variable(variable_name="magnet_setback_ratio", lower=0.1, upper=0.2, resolution=0.001, unit="")
    simulation.magnet_thick_ratio = design.random_variable(variable_name="magnet_thick_ratio", lower=0.2, upper=0.5, resolution=0.001, unit="")
    simulation.magnet_space_height_ratio = design.random_variable(variable_name="magnet_space_height_ratio", lower=0.8, upper=1.0, resolution=0.001, unit="")
    simulation.magnet_height_ratio = design.random_variable(variable_name="magnet_height_ratio", lower=0.8, upper=1.0, resolution=0.001, unit="")
    simulation.magnet_setback = simulation.rotor_thick * simulation.magnet_setback_ratio
    simulation.magnet_thick = simulation.rotor_thick * simulation.magnet_thick_ratio
    simulation.magnet_height = ((simulation.rotor_radius - simulation.magnet_setback - simulation.magnet_thick) * math.cos(math.pi/simulation.pole_num) - simulation.magnet_shield_thick)

    design["rotor_radius"] = f"stator_inner_radius - rotator_gap"
    design["shaft_radius"] = f"rotor_radius * shaft_ratio"
    design["rotor_thick"] = f"rotor_radius - shaft_radius"
    design["magnet_setback"] = f"(rotor_thick * magnet_setback_ratio)"
    design["magnet_thick"] = f"(rotor_thick * magnet_thick_ratio)"
    design["magnet_height"] = f"(((rotor_radius - magnet_setback - magnet_thick) * cos(360deg/pole_num/2)) - magnet_shield_thick)"



 



    

    

  
    return _geometry_input_dataframe(simulation)
