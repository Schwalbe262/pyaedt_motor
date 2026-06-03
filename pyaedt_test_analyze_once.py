# Auto-generated setup-only runner from pyaedt_test.ipynb.

# It intentionally runs modeling + PPT/PyAEDT setup cells only.



# --- notebook cell 0 ---

import sys
import traceback
import logging
import os
import contextlib

# 경로 설정 - 플랫폼에 따라 다르게 처리
if os.name == 'nt':  # Windows
    sys.path.insert(0, r"Y:/git/pyaedt_library/src/")
else:  # Linux/Unix
    # Linux 서버 경로들 시도
    possible_paths = [
        # r"/gpfs/home1/r1jae262/jupyter/git/pyaedt_library/src/",
        r"../pyaedt_library/src/",
        os.path.join(os.path.dirname(__file__), "../git/pyaedt_library/src/"),
    ]
    for path in possible_paths:
        if os.path.exists(path):
            sys.path.insert(0, path)
            break

import pyaedt_module
from pyaedt_module.core import pyDesktop
import os
import time
from datetime import datetime

import math
import copy

import pandas as pd

import platform
import csv

from scipy.signal import find_peaks

from module.variable import set_variable






@contextlib.contextmanager
def locked_counter_file(file_path):
    """Lock simulation_num.txt using only the Python standard library."""
    if os.name == "nt":
        import msvcrt

        with open(file_path, "r+", encoding="utf-8") as file:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield file
            finally:
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with open(file_path, "r+", encoding="utf-8") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield file
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class Simulation() :

    def __init__(self, desktop=None) :

        self.NUM_CORE = 4
        self.NUM_TASK = 1

        # Desktop? ???? ??(with ??)?? ???? ?? ??
        # (?? ? ???? close/delete? Desktop ??? ?????? ??? ??? ??)
        self.desktop = desktop

    def create_simulation_name(self):

        file_path = "./simulation_num.txt"

        # ??? ???? ??? ??
        if not os.path.exists(file_path):
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("1")

        with locked_counter_file(file_path) as file:
            content_raw = file.read().strip()
            content = int(content_raw) if content_raw else 1
            self.num = content
            self.PROJECT_NAME = f"simulation{content}"
            content += 1

            file.seek(0)
            file.truncate()
            file.write(str(content))
            file.flush()
            os.fsync(file.fileno())

    def set_variable(self, design):
        return set_variable(self, design)


# --- notebook cell 1 ---

GUI = False
desktop = None

desktop = pyDesktop(version=None, non_graphical=GUI, close_on_exit=False, new_desktop=True)

sim1 = Simulation(desktop=desktop)

sim1.create_simulation_name()

# simulation 디렉토리 생성 (존재하지 않으면)
simulation_dir = "./simulation"
if not os.path.exists(simulation_dir):
    os.makedirs(simulation_dir, exist_ok=True)

# 절대 경로로 변환
project_path = os.path.abspath(os.path.join(simulation_dir, sim1.PROJECT_NAME))

# desktop이 None이거나 유효하지 않은지 확인
if sim1.desktop is None:
    raise RuntimeError("Desktop instance is None. Cannot create project.")

try:
    project1 = sim1.desktop.create_project(path=project_path, name=sim1.PROJECT_NAME)
except Exception as e:
    error_msg = f"Failed to create project '{sim1.PROJECT_NAME}' at path '{project_path}': {e}\n"
    print(error_msg, file=sys.stderr)
    sys.stderr.flush()
    raise


# --- notebook cell 2 ---


design1 = project1.create_design(name="IPMSM", solver="Maxwell 2D", solution="Transient")

sim1.project = project1

input_data = sim1.set_variable(design1)


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
        ("stator_outer_radius - stator_back_yoke_thick - stator_teeth_length",  sim1.stator_teeth_width / 2,  0),
        ("stator_outer_radius - stator_back_yoke_thick - stator_teeth_length", -sim1.stator_teeth_width / 2,  0),
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
    """면 중심 우선, 안 되면 바운딩 박스 중심 (2D Maxwell)."""
    f0 = w.faces[0]
    if not isinstance(f0, int):
        c = f0.center
        return float(c[0]), float(c[1])
    bb = w.bounding_box
    # PyAEDT: 보통 [xmin, ymin, zmin, xmax, ymax, zmax] (6개)
    if isinstance(bb, (list, tuple)) and len(bb) >= 6:
        return (bb[0] + bb[3]) / 2.0, (bb[1] + bb[4]) / 2.0
    if isinstance(bb, (list, tuple)) and len(bb) >= 4:
        return (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
    raise TypeError("bounding_box 형식을 확인하세요:", type(bb), bb)


def angle_ccw_from_pos_x(xy):
    x, y = xy
    ang = math.atan2(y, x)
    if ang < 0:
        ang += 2 * math.pi
    return ang


# 1) 1→2→3→4 사분면 방향(반시계, +x 기준)으로 정렬
sorted_windings = sorted(
    windings,
    key=lambda w: angle_ccw_from_pos_x(winding_center_xy(w)),
)

# 2) 이름을 winding1, winding2, ... (임시 이름으로 한 번에 바꾸지 않기 위해 2패스)
for i, w in enumerate(sorted_windings):
    w.name = f"__w_tmp_{i}"

for i, w in enumerate(sorted_windings, start=1):
    w.name = f"winding{i}"

# 이후 코드에서 같은 리스트를 쓰려면
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
    """면 중심 우선, 안 되면 바운딩 박스 중심."""
    f0 = obj.faces[0]
    if not isinstance(f0, int):
        c = f0.center
        return float(c[0]), float(c[1])
    bb = obj.bounding_box
    if isinstance(bb, (list, tuple)) and len(bb) >= 6:
        return (bb[0] + bb[3]) / 2.0, (bb[1] + bb[4]) / 2.0
    if isinstance(bb, (list, tuple)) and len(bb) >= 4:
        return (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
    raise TypeError("bounding_box 형식을 확인하세요:", type(bb), bb)
# y=0에 가장 가까운 조각 하나
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

# p0, p1 이 None이 아니면
x0, y0, z0 = p0[0], p0[1], p0[2]
x1, y1, z1 = p1[0], p1[1], p1[2]

magnet_height = abs(y0)

magnet_N = design1.modeler.create_rectangle(
    origin=["rotor_radius - magnet_setback", f"-{magnet_height}mm*magnet_height_ratio", 0], 
    sizes=["-magnet_thick", f"{magnet_height}mm*magnet_height_ratio*2"], 
    name="magnet", 
    material="iron")
magnet_N.color = [255, 0, 0]
magnet_N.transparency = 0
design1.modeler.delete(assignment=barrier)


design1.modeler.duplicate_around_axis(assignment=[magnet_space], axis="Z", angle="(360/pole_num)deg", clones="pole_num", create_new_objects=False)
design1.modeler.duplicate_around_axis(assignment=[magnet_N], axis="Z", angle="(360/pole_num*2)deg", clones="pole_num", create_new_objects=False)


design1.modeler.copy(assignment=magnet_N)
magnet_S = design1.modeler.paste()
magnet_S = design1.modeler.get_object_from_name(magnet_S[0])
magnet_S.color = [0, 0, 255]
magnet_S.transparency = 0
design1.modeler.rotate(assignment=magnet_S, axis="Z", angle="(360/pole_num)deg")


design1.modeler.subtract(blank_list=rotator, tool_list=[magnet_space], keep_originals=True)
design1.modeler.subtract(blank_list=magnet_space, tool_list=[magnet_N, magnet_S], keep_originals=True)


# --- notebook cell 4 ---

import importlib
import module.ipmsm_ppt_setup as ipmsm_ppt_setup

ipmsm_ppt_setup = importlib.reload(ipmsm_ppt_setup)
IPMSMPPTSpec = ipmsm_ppt_setup.IPMSMPPTSpec
configure_ipmsm_from_ppt = ipmsm_ppt_setup.configure_ipmsm_from_ppt

# PPT slide 6 specification and setup defaults.
ppt_spec = IPMSMPPTSpec(
    pole_number=8,
    slot_number=12,
    symmetry_factor=4,
    base_rpm=1200,
    i_peak_a=137.8,
    beta_deg=0,                # Sweep this for MTPA.
    series_turns_per_phase=48,
    turns_per_coil_side=12,    # 48 phase turns / 4 coil sides per phase for 12S/8P.
    stack_length_mm=49.45,
    phase_resistance_ohm=0.01, # PPT voltage-limit example uses 0.01 ohm.
    vdc_v=200,
    initial_position_deg=-22.5,
    transient_periods=10,
    steps_per_period=90,
    core_material="27PNF1500_CustomCoreLoss",
    magnet_material="NdFeB_1.25T",
)

# These names come from the modeling cell above. The helper also auto-detects by prefix,
# but explicit groups make the setup more predictable.
object_groups = {
    "stator": [main_stator.name],
    "rotor": [rotator.name],
    "shaft": [shaft.name],
    "magnets": [magnet_N.name, magnet_S.name],
    "windings": [w.name for w in windings],
    "region": ["Region"],
    "band": ["Band"],
}

ppt_setup_result = configure_ipmsm_from_ppt(
    design1,
    object_groups=object_groups,
    spec=ppt_spec,
    operation="sin_current",      # Use "no_load" for Back EMF / cogging torque.
    use_periodic_boundary=False,  # Set True only for a split periodic sector model.
    create_missing_region=True,
    create_missing_band=True,
    create_reports=True,
    clear_existing=True,          # Remove only previous PPT_* setup artifacts before rebuilding.
    analyze=True,                # One-off solve test generated by Codex.
    cores=sim1.NUM_CORE,
)

ppt_setup_result


# --- setup result summary ---
try:
    print("\n=== PPT SETUP RESULT KEYS ===")
    print(sorted(ppt_setup_result.keys()))
    for key, value in ppt_setup_result.items():
        print(f"\n[{key}]")
        print(value)
except Exception as exc:
    print(f"Could not print ppt_setup_result: {exc}")

try:
    project1.save()
    print("\nProject saved.")
except Exception as exc:
    print(f"Project save skipped: {exc}")
