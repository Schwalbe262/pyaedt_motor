"""
HFSS analysis setup and execution functions.
"""

import pandas as pd
import numpy as np
import time


def HFSS_analyze(simulation, project, design):
    """
    Set up and execute HFSS analysis.
    
    Args:
        simulation: Simulation object containing project, NUM_CORE, NUM_TASK attributes
        design: pyDesign object to analyze
        
    Returns:
        setup: The created setup object
    """
    setup = design.create_setup("Setup1")
    setup.props["Frequency"] = "10MHz"
    setup["MaximumPasses"] = 12
    setup["MaxDeltaS"] = 0.001

    oModule = design.GetModule("AnalysisSetup")

    sweep1 = oModule.InsertFrequencySweep("Setup1", 
        [
            "NAME:Sweep",
            "IsEnabled:="		, True,
            "RangeType:="		, "LinearCount",
            "RangeStart:="		, "50kHz",
            "RangeEnd:="		, "1MHz",
            "RangeCount:="		, 100,
            [
                "NAME:SweepRanges",
                [
                    "NAME:Subrange",
                    "RangeType:="		, "LinearCount",
                    "RangeStart:="		, "1MHz",
                    "RangeEnd:="		, "10MHz",
                    "RangeCount:="		, 100
                ],
                [
                    "NAME:Subrange",
                    "RangeType:="		, "LinearCount",
                    "RangeStart:="		, "10MHz",
                    "RangeEnd:="		, "500MHz",
                    "RangeCount:="		, 400
                ]
            ],
            "Type:="		, "Fast",
            "SaveFields:="		, True,
            "SaveRadFields:="	, False,
            "GenerateFieldsForAllFreqs:=", False
        ])

    project.save()
    design.analyze(setup=f"{setup.name}", cores=simulation.NUM_CORE, tasks=simulation.NUM_TASK)

    return setup



def get_HFSS_results(project, design) :

    project_dir = project.path # csv 파일 저장 위치치

    # resonant frequency / coupling coefficient data

    result_expressions = []
    result_expressions.append(f"mag(Zt(Tx_port_T1,Tx_port_T1))")
    result_expressions.append(f"mag(Zt(Rx_port_T1,Rx_port_T1))")
    result_expressions.append(f"mag(Zt(Tx_port_T1,Rx_port_T1))")
    result_expressions.append(f"im(Zt(Tx_port_T1,Rx_port_T1))/sqrt(im(Zt(Tx_port_T1,Tx_port_T1))*im(Zt(Rx_port_T1,Rx_port_T1)))")

    report1 = design.post.create_report(expressions=result_expressions, setup_sweep_name=None, domain='Sweep', 
                                    variations=None, primary_sweep_variable=None, secondary_sweep_variable=None, 
                                    report_category="Terminal Solution Data", plot_type='Rectangular Plot', context=None, subdesign_id=None, polyline_points=1001, plot_name="Impedance Data")
    time.sleep(1)
    sim_data1 = design.post.export_report_to_csv(project_dir=project_dir, plot_name=report1.plot_name, uniform=False, start=None, end=None, step=None, use_trace_number_format=False)
    data1 = pd.read_csv(sim_data1)
    data1 = design.post_processing.data_preprocessing(data1)

    data_freq = data1["Freq [Hz]"]
    data_ZTT = data1["mag(Zt(Tx_port_T1,Tx_port_T1)) []"]
    data_ZRR = data1["mag(Zt(Rx_port_T1,Rx_port_T1)) []"]
    data_ZTR = data1["mag(Zt(Tx_port_T1,Rx_port_T1)) []"]
    data_k = data1["im(Zt(Tx_port_T1,Rx_port_T1))/sqrt(im(Zt(Tx_port_T1,Tx_port_T1))*im(Zt(Rx_port_T1,Rx_port_T1))) []"]


    freq1, peak1 = design.post_processing.detect_peak(freq=data_freq, data=data_ZTT) # Z11 공진 주파수
    freq2, peak2 = design.post_processing.detect_peak(freq=data_freq, data=data_ZRR) # Z22 공진주파수
    freq3, peak3 = design.post_processing.detect_peak(freq=data_freq, data=data_ZTR) # Z12 공진주파수

    # freq1, freq2, freq3이 list인 경우 0번 인덱스 값을 사용하도록 처리
    # peak 여러개 검출 시 가장 낮은 주파수 값 사용용
    if isinstance(freq1, np.ndarray):
        freq1 = round(float(freq1[0]), 3)
        peak1 = round(float(peak1[0]), 3)
    if isinstance(freq2, np.ndarray):
        freq2 = round(float(freq2[0]), 3)
        peak2 = round(float(peak2[0]), 3)
    if isinstance(freq3, np.ndarray):
        freq3 = round(float(freq3[0]), 3)
        peak3 = round(float(peak3[0]), 3)



    k_10k = abs(design.post_processing.get_frequency_data(10e+3, data_freq, data_k))
    k_100k = abs(design.post_processing.get_frequency_data(100e+3, data_freq, data_k))
    k_1M = abs(design.post_processing.get_frequency_data(1e+6, data_freq, data_k))
    k_10M = abs(design.post_processing.get_frequency_data(10e+6, data_freq, data_k))
    k_15M = abs(design.post_processing.get_frequency_data(15e+6, data_freq, data_k))
    k_20M = abs(design.post_processing.get_frequency_data(20e+6, data_freq, data_k))
    k_25M = abs(design.post_processing.get_frequency_data(25e+6, data_freq, data_k))
    k_30M = abs(design.post_processing.get_frequency_data(30e+6, data_freq, data_k))
    k_100M = abs(design.post_processing.get_frequency_data(100e+6, data_freq, data_k))

    columns = ['TT_resonant_freq', 'TT_resonant_Z', 'RR_resonant_freq', 'RR_resonant_Z', 'TR_resonant_freq', 'TR_resonant_Z']
    resonant_raw = [freq1/1e+6, peak1, freq2/1e+6, peak2, freq3/1e+6, peak3]
    pd_resonant = pd.DataFrame([resonant_raw], columns=columns)

    columns = ['k_10k', 'k_100k', 'k_1M', 'k_10M', 'k_15M', 'k_20M', 'k_25M', 'k_30M', 'k_100M']
    k_raw = [k_10k, k_100k, k_1M, k_10M, k_15M, k_20M, k_25M, k_30M, k_100M]
    pd_k = pd.DataFrame([k_raw], columns=columns)

    
    

    # inductance data

    result_expressions = []
    result_expressions.append(f"im(Zt(Tx_port_T1,Tx_port_T1))/(2*pi*freq)")
    result_expressions.append(f"im(Zt(Rx_port_T1,Rx_port_T1))/(2*pi*freq)")
    result_expressions.append(f"im(Zt(Tx_port_T1,Rx_port_T1))/(2*pi*freq)")

    report2 = design.post.create_report(expressions=result_expressions, setup_sweep_name=None, domain='Sweep', 
                                    variations=None, primary_sweep_variable=None, secondary_sweep_variable=None, 
                                    report_category="Terminal Solution Data", plot_type='Rectangular Plot', context=None, subdesign_id=None, polyline_points=1001, plot_name="Inductance Data")
    time.sleep(1)
    sim_data2 = design.post.export_report_to_csv(project_dir=project_dir, plot_name=report2.plot_name, uniform=False, start=None, end=None, step=None, use_trace_number_format=False)
    data2 = pd.read_csv(sim_data2)
    data2 = design.post_processing.data_preprocessing(data2)

    data_freq = data2["Freq [Hz]"]
    data_LTT = data2["im(Zt(Tx_port_T1,Tx_port_T1))/(2*pi*freq) []"]
    data_LRR = data2["im(Zt(Rx_port_T1,Rx_port_T1))/(2*pi*freq) []"]
    data_LTR = data2["im(Zt(Tx_port_T1,Rx_port_T1))/(2*pi*freq) []"]


    # 단위는 nH로 변환  

    LT_10k = design.post_processing.get_frequency_data(10e+3, data_freq, data_LTT) * 1e+9
    LT_100k = design.post_processing.get_frequency_data(100e+3, data_freq, data_LTT) * 1e+9
    LT_1M = design.post_processing.get_frequency_data(1e+6, data_freq, data_LTT) * 1e+9
    LT_10M = design.post_processing.get_frequency_data(10e+6, data_freq, data_LTT) * 1e+9
    LT_15M = design.post_processing.get_frequency_data(15e+6, data_freq, data_LTT) * 1e+9
    LT_20M = design.post_processing.get_frequency_data(20e+6, data_freq, data_LTT) * 1e+9
    LT_25M = design.post_processing.get_frequency_data(25e+6, data_freq, data_LTT) * 1e+9
    LT_30M = design.post_processing.get_frequency_data(30e+6, data_freq, data_LTT) * 1e+9
    LT_100M = design.post_processing.get_frequency_data(100e+6, data_freq, data_LTT) * 1e+9

    LR_10k = design.post_processing.get_frequency_data(10e+3, data_freq, data_LRR) * 1e+9
    LR_100k = design.post_processing.get_frequency_data(100e+3, data_freq, data_LRR) * 1e+9
    LR_1M = design.post_processing.get_frequency_data(1e+6, data_freq, data_LRR) * 1e+9
    LR_10M = design.post_processing.get_frequency_data(10e+6, data_freq, data_LRR) * 1e+9
    LR_15M = design.post_processing.get_frequency_data(15e+6, data_freq, data_LRR) * 1e+9
    LR_20M = design.post_processing.get_frequency_data(20e+6, data_freq, data_LRR) * 1e+9
    LR_25M = design.post_processing.get_frequency_data(25e+6, data_freq, data_LRR) * 1e+9
    LR_30M = design.post_processing.get_frequency_data(30e+6, data_freq, data_LRR) * 1e+9
    LR_100M = design.post_processing.get_frequency_data(100e+6, data_freq, data_LRR) * 1e+9

    LM_10k = design.post_processing.get_frequency_data(10e+3, data_freq, data_LTR) * 1e+9
    LM_100k = design.post_processing.get_frequency_data(100e+3, data_freq, data_LTR) * 1e+9
    LM_1M = design.post_processing.get_frequency_data(1e+6, data_freq, data_LTR) * 1e+9
    LM_10M = design.post_processing.get_frequency_data(10e+6, data_freq, data_LTR) * 1e+9
    LM_15M = design.post_processing.get_frequency_data(15e+6, data_freq, data_LTR) * 1e+9
    LM_20M = design.post_processing.get_frequency_data(20e+6, data_freq, data_LTR) * 1e+9
    LM_25M = design.post_processing.get_frequency_data(25e+6, data_freq, data_LTR) * 1e+9
    LM_30M = design.post_processing.get_frequency_data(30e+6, data_freq, data_LTR) * 1e+9
    LM_100M = design.post_processing.get_frequency_data(100e+6, data_freq, data_LTR) * 1e+9

    columns = ['LT_10k', 'LT_100k', 'LT_1M', 'LT_10M', 'LT_15M', 'LT_20M', 'LT_25M', 'LT_30M', 'LT_100M']
    L_raw = [LT_10k, LT_100k, LT_1M, LT_10M, LT_15M, LT_20M, LT_25M, LT_30M, LT_100M]
    pd_L = pd.DataFrame([L_raw], columns=columns)

    columns = ['LR_10k', 'LR_100k', 'LR_1M', 'LR_10M', 'LR_15M', 'LR_20M', 'LR_25M', 'LR_30M', 'LR_100M']
    LR_raw = [LR_10k, LR_100k, LR_1M, LR_10M, LR_15M, LR_20M, LR_25M, LR_30M, LR_100M]
    pd_LR = pd.DataFrame([LR_raw], columns=columns)

    columns = ['LM_10k', 'LM_100k', 'LM_1M', 'LM_10M', 'LM_15M', 'LM_20M', 'LM_25M', 'LM_30M', 'LM_100M']
    LM_raw = [LM_10k, LM_100k, LM_1M, LM_10M, LM_15M, LM_20M, LM_25M, LM_30M, LM_100M]
    pd_LM = pd.DataFrame([LM_raw], columns=columns) 

    

    # S-parameter data

    result_expressions = []
    result_expressions.append(f"dB(St(Tx_port_T1,Tx_port_T1))")
    result_expressions.append(f"dB(St(Rx_port_T1,Rx_port_T1))")
    result_expressions.append(f"dB(St(Tx_port_T1,Rx_port_T1))")

    report3 = design.post.create_report(expressions=result_expressions, setup_sweep_name=None, domain='Sweep', 
                                    variations=None, primary_sweep_variable=None, secondary_sweep_variable=None, 
                                    report_category="Terminal Solution Data", plot_type='Rectangular Plot', context=None, subdesign_id=None, polyline_points=1001, plot_name="S-parameter Data")
    time.sleep(1)
    sim_data3 = design.post.export_report_to_csv(project_dir=project_dir, plot_name=report3.plot_name, uniform=False, start=None, end=None, step=None, use_trace_number_format=False)
    data3 = pd.read_csv(sim_data3)
    data3 = design.post_processing.data_preprocessing(data3)

    data_freq = data3["Freq [Hz]"]
    data_S11 = data3["dB(St(Tx_port_T1,Tx_port_T1)) []"]
    data_S22 = data3["dB(St(Rx_port_T1,Rx_port_T1)) []"]
    data_S12 = data3["dB(St(Tx_port_T1,Rx_port_T1)) []"]

    
    # S11 데이터 추출: 10kHz, 100kHz, 1M, 10M, 15M, 20M, 25M, 30M, 100M
    S11_10k = abs(design.post_processing.get_frequency_data(10e+3, data_freq, data_S11))
    S11_100k = abs(design.post_processing.get_frequency_data(100e+3, data_freq, data_S11))
    S11_1M = abs(design.post_processing.get_frequency_data(1e+6, data_freq, data_S11))
    S11_10M = abs(design.post_processing.get_frequency_data(10e+6, data_freq, data_S11))
    S11_15M = abs(design.post_processing.get_frequency_data(15e+6, data_freq, data_S11))
    S11_20M = abs(design.post_processing.get_frequency_data(20e+6, data_freq, data_S11))
    S11_25M = abs(design.post_processing.get_frequency_data(25e+6, data_freq, data_S11))
    S11_30M = abs(design.post_processing.get_frequency_data(30e+6, data_freq, data_S11))
    S11_100M = abs(design.post_processing.get_frequency_data(100e+6, data_freq, data_S11))

    # S22 데이터 추출: 10kHz, 100kHz, 1M, 10M, 15M, 20M, 25M, 30M, 100M
    S22_10k = abs(design.post_processing.get_frequency_data(10e+3, data_freq, data_S22))
    S22_100k = abs(design.post_processing.get_frequency_data(100e+3, data_freq, data_S22))
    S22_1M = abs(design.post_processing.get_frequency_data(1e+6, data_freq, data_S22))
    S22_10M = abs(design.post_processing.get_frequency_data(10e+6, data_freq, data_S22))
    S22_15M = abs(design.post_processing.get_frequency_data(15e+6, data_freq, data_S22))
    S22_20M = abs(design.post_processing.get_frequency_data(20e+6, data_freq, data_S22))
    S22_25M = abs(design.post_processing.get_frequency_data(25e+6, data_freq, data_S22))
    S22_30M = abs(design.post_processing.get_frequency_data(30e+6, data_freq, data_S22))
    S22_100M = abs(design.post_processing.get_frequency_data(100e+6, data_freq, data_S22))

    # S12 데이터 추출: 10kHz, 100kHz, 1M, 10M, 15M, 20M, 25M, 30M, 100M
    S12_10k = abs(design.post_processing.get_frequency_data(10e+3, data_freq, data_S12))
    S12_100k = abs(design.post_processing.get_frequency_data(100e+3, data_freq, data_S12))
    S12_1M = abs(design.post_processing.get_frequency_data(1e+6, data_freq, data_S12))
    S12_10M = abs(design.post_processing.get_frequency_data(10e+6, data_freq, data_S12))
    S12_15M = abs(design.post_processing.get_frequency_data(15e+6, data_freq, data_S12))
    S12_20M = abs(design.post_processing.get_frequency_data(20e+6, data_freq, data_S12))
    S12_25M = abs(design.post_processing.get_frequency_data(25e+6, data_freq, data_S12))
    S12_30M = abs(design.post_processing.get_frequency_data(30e+6, data_freq, data_S12))
    S12_100M = abs(design.post_processing.get_frequency_data(100e+6, data_freq, data_S12))

    freq1, peak1 = design.post_processing.detect_max(freq=data_freq, data=abs(data_S11))
    freq2, peak2 = design.post_processing.detect_max(freq=data_freq, data=abs(data_S22))
    freq3, peak3 = design.post_processing.detect_min(freq=data_freq, data=abs(data_S12))

    columns = ['S11_10k', 'S11_100k', 'S11_1M', 'S11_10M', 'S11_15M', 'S11_20M', 'S11_25M', 'S11_30M', 'S11_100M']  
    S11_raw = [S11_10k, S11_100k, S11_1M, S11_10M, S11_15M, S11_20M, S11_25M, S11_30M, S11_100M]
    pd_S11 = pd.DataFrame([S11_raw], columns=columns)

    columns = ['S22_10k', 'S22_100k', 'S22_1M', 'S22_10M', 'S22_15M', 'S22_20M', 'S22_25M', 'S22_30M', 'S22_100M']
    S22_raw = [S22_10k, S22_100k, S22_1M, S22_10M, S22_15M, S22_20M, S22_25M, S22_30M, S22_100M]
    pd_S22 = pd.DataFrame([S22_raw], columns=columns)

    columns = ['S12_10k', 'S12_100k', 'S12_1M', 'S12_10M', 'S12_15M', 'S12_20M', 'S12_25M', 'S12_30M', 'S12_100M']
    S12_raw = [S12_10k, S12_100k, S12_1M, S12_10M, S12_15M, S12_20M, S12_25M, S12_30M, S12_100M]
    pd_S12 = pd.DataFrame([S12_raw], columns=columns)       

    columns = ['S11_peak_freq', 'S11_peak_dB', 'S22_peak_freq', 'S22_peak_dB', 'S12_peak_freq', 'S12_peak_dB']
    resonant_raw = [freq1/1e+6, peak1, freq2/1e+6, peak2, freq3/1e+6, peak3]
    pd_Speak = pd.DataFrame([resonant_raw], columns=columns)


    pd_data = pd.concat([pd_resonant, pd_k, pd_L, pd_LR, pd_LM, pd_S11, pd_S22, pd_S12, pd_Speak], axis=1)

    return pd_data



def simulation_report(design, start_time):

    pass_number, tetrahedra, delta_S = design.get_report_data(setup=design.setup_names[0])

    end_time = time.time()
    execution_time = int(end_time - start_time)

    columns = ['time', 'pass_number', 'tetrahedra', 'delta_S']
    sim_result_raw = [execution_time, pass_number, tetrahedra, delta_S]
    sim_result = pd.DataFrame([sim_result_raw], columns=columns)

    return sim_result