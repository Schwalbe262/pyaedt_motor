import os
import time
import pandas as pd


def create_HFSS_link_model(link_name="HFSS_link_model", project=None, HFSS_design=None, circuit_design=None, Tx_port=None, Rx_port=None):



    image_file = os.path.join(project.path, "image.gif")
    ModTime = int(time.time())

    oDefinitionManager = project.GetDefinitionManager()
    oModelManager = oDefinitionManager.GetManager("Model")
    oComponentManager = oDefinitionManager.GetManager("Component")

    Tx_terminal_name = list(Tx_port.children.keys())[0]
    Rx_terminal_name = list(Rx_port.children.keys())[0]

    # oModelManager로 추가
    params1 = [
        "NAME:HFSS_design1",
        "Name:="		, "HFSS_design1",
        "ModTime:="		, f"{ModTime}",
        "Library:="		, "",
        "LibLocation:="		, "Project",
        "ModelType:="		, "hfss",
        "Description:="		, "",
        "ImageFile:="		, f"{image_file}",
        "SymbolPinConfiguration:=", 0,
        [
            "NAME:PortInfoBlk"
        ],
        [
            "NAME:PortOrderBlk"
        ],
        "DesignName:="		, f"{HFSS_design.name}",
        "SolutionName:="	, "Setup1 : Sweep",
        "NewToOldMap:="		, [],
        "OldToNewMap:="		, [],
        "PinNames:="		, [f"{Rx_terminal_name}",f"{Tx_terminal_name}"],
        [
            "NAME:DesignerCustomization",
            "DCOption:="		, 0,
            "InterpOption:="	, 0,
            "ExtrapOption:="	, 1,
            "Convolution:="		, 0,
            "Passivity:="		, 0,
            "Reciprocal:="		, False,
            "ModelOption:="		, "",
            "DataType:="		, 1
        ],
        [
            "NAME:NexximCustomization",
            "DCOption:="		, 3,
            "InterpOption:="	, 1,
            "ExtrapOption:="	, 3,
            "Convolution:="		, 0,
            "Passivity:="		, 0,
            "Reciprocal:="		, False,
            "ModelOption:="		, "",
            "DataType:="		, 2
        ],
        [
            "NAME:HSpiceCustomization",
            "DCOption:="		, 1,
            "InterpOption:="	, 2,
            "ExtrapOption:="	, 3,
            "Convolution:="		, 0,
            "Passivity:="		, 0,
            "Reciprocal:="		, False,
            "ModelOption:="		, "",
            "DataType:="		, 3
        ],
        "NoiseModelOption:="	, "External",
        "WB_SystemID:="		, f"{HFSS_design.name}",
        "IsWBModel:="		, False,
        "filename:="		, "",
        "numberofports:="	, 2,
        "Simulate:="		, False,
        "CloseProject:="	, False,
        "SaveProject:="		, True,
        "InterpY:="		, True,
        "InterpAlg:="		, "auto",
        "IgnoreDepVars:="	, False,
        "Renormalize:="		, False,
        "RenormImpedance:="	, 50
    ]


    # oComponentManager로 추가
    params2 = [
        f"NAME:{link_name}",
        "Info:="		, [			"Type:="		, 8,			"NumTerminals:="	, 2,			"DataSource:="		, "",			"ModifiedOn:="		, ModTime,			"Manufacturer:="	, "",			"Symbol:="		, "",			"ModelNames:="		, "",			"Footprint:="		, "",			"Description:="		, "",			"InfoTopic:="		, "",			"InfoHelpFile:="	, "",			"IconFile:="		, "hfss.bmp",			"Library:="		, "",			"OriginalLocation:="	, "Project",			"IEEE:="		, "",			"Author:="		, "",			"OriginalAuthor:="	, "",			"CreationDate:="	, 1769047810,			"ExampleFile:="		, "",			"HiddenComponent:="	, 0,			"CircuitEnv:="		, 0,			"GroupID:="		, 0],
        "CircuitEnv:="		, 0,
        "Refbase:="		, "S",
        "NumParts:="		, 1,
        "ModSinceLib:="		, False,
        "Terminal:="		, [f"{Rx_terminal_name}",f"{Rx_terminal_name}","A",False,2,1,"","Electrical","0"],
        "Terminal:="		, [f"{Tx_terminal_name}",f"{Tx_terminal_name}","A",False,3,1,"","Electrical","0"],
        [
            "NAME:Properties",
            "TextProp:="		, ["Owner","RD","","HFSS"]
        ],
        "CompExtID:="		, 5,
        [
            "NAME:Parameters",
            "VariableProp:="	, ["Tx_outer_x","D","",f"{HFSS_design.variables['Tx_outer_x']}"],
            "VariableProp:="	, ["Rx_outer_x","D","",f"{HFSS_design.variables['Rx_outer_x']}"],
            "VariableProp:="	, ["Tx_inner","D","",f"{HFSS_design.variables['Tx_inner']}"],
            "VariableProp:="	, ["Tx_fillet","D","",f"{HFSS_design.variables['Tx_fillet']}"],
            "VariableProp:="	, ["Tx_fill_factor","D","",f"{HFSS_design.variables['Tx_fill_factor']}"],
            "VariableProp:="	, ["Rx_inner","D","",f"{HFSS_design.variables['Rx_inner']}"],
            "VariableProp:="	, ["Rx_fillet","D","",f"{HFSS_design.variables['Rx_fillet']}"],
            "VariableProp:="	, ["Rx_fill_factor","D","",f"{HFSS_design.variables['Rx_fill_factor']}"],
            "VariableProp:="	, ["Tx_turns","D","",f"{HFSS_design.variables['Tx_turns']}"],
            "VariableProp:="	, ["Rx_turns","D","",f"{HFSS_design.variables['Rx_turns']}"],
            "VariableProp:="	, ["Tx_layer","D","",f"{HFSS_design.variables['Tx_layer']}"],
            "VariableProp:="	, ["Rx_layer","D","",f"{HFSS_design.variables['Rx_layer']}"],
            "VariableProp:="	, ["Tx_outer_y","HD","",f"{HFSS_design.variables['Tx_outer_y']}"],
            "VariableProp:="	, ["Rx_outer_y","HD","",f"{HFSS_design.variables['Rx_outer_y']}"],
            "VariableProp:="	, ["Tx_theta1","HD","",f"{HFSS_design.variables['Tx_theta1']}"],
            "VariableProp:="	, ["Tx_theta2","HD","",f"{HFSS_design.variables['Tx_theta2']}"],
            "VariableProp:="	, ["Rx_theta1","HD","",f"{HFSS_design.variables['Rx_theta1']}"],
            "VariableProp:="	, ["Rx_theta2","HD","",f"{HFSS_design.variables['Rx_theta2']}"],
            "VariableProp:="	, ["PCB_thickness","D","",f"{HFSS_design.variables['PCB_thickness']}"],
            "VariableProp:="	, ["Tx_Tx_gap","D","",f"{HFSS_design.variables['Tx_Tx_gap']}"],
            "VariableProp:="	, ["Rx_Rx_gap","D","",f"{HFSS_design.variables['Rx_Rx_gap']}"],
            "VariableProp:="	, ["Tx_Rx_gap","D","",f"{HFSS_design.variables['Tx_Rx_gap']}"],
            "TextProp:="		, ["ModelName","RD","","FieldSolver"],
            "MenuProp:="		, ["CoSimulator","SD","","Default",0],
            "ButtonProp:="		, ["CosimDefinition","SD","","Edit","Edit",40501,				"ButtonPropClientData:=", []]
        ],
        [
            "NAME:CosimDefinitions",
            [
                "NAME:CosimDefinition",
                "CosimulatorType:="	, 103,
                "CosimDefName:="	, "Default",
                "IsDefinition:="	, True,
                "Connect:="		, True,
                "ModelDefinitionName:="	, f"{link_name}",
                "ShowRefPin2:="		, 2,
                "LenPropName:="		, ""
            ],
            "DefaultCosim:="	, "Default"
        ]
    ]
        
    oModelManager.Add(params1)
    oComponentManager.Add(params2)
    circuit_design.AddCompInstance(f"{link_name}")



def simulation_setup(circuit_design=None):

    oModule = circuit_design.GetModule("SimSetup")
    oModule.AddLinearNetworkAnalysis(
        [
            "NAME:SimSetup",
            "DataBlockID:="		, 16,
            "OptionName:="		, "(Default Options)",
            "AdditionalOptions:="	, "",
            "AlterBlockName:="	, "",
            "FilterText:="		, "",
            "AnalysisEnabled:="	, 1,
            "HasTDRComp:="		, 0,
            [
                "NAME:OutputQuantities"
            ],
            [
                "NAME:NoiseOutputQuantities"
            ],
            "Name:="		, "LinearFrequency",
            "LinearFrequencyData:="	, [False,0.1,False,"",False],
            [
                "NAME:SweepDefinition",
                "Variable:="		, "Freq",
                "Data:="		, "10MHz",
                "OffsetF1:="		, False,
                "Synchronize:="		, 0
            ]
        ])



def create_schematic(circuit_design=None):

    oDesign = circuit_design
    oEditor = oDesign.SetActiveEditor("SchematicEditor")

    oEditor.CreateIPort(
        [
            "NAME:IPortProps",
            "Name:="		, "Port1",
            "Id:="			, 3
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1,
            "X:="			, 0.0127,
            "Y:="			, 0.0127,
            "Angle:="		, 0,
            "Flip:="		, False
        ])
    oDesign.UpdateSources(
        [
            "NAME:NexximSources",
            [
                "NAME:NexximSources",
                [
                    "NAME:Data"
                ]
            ]
        ], 
        [
            "NAME:ComponentConfigurationData",
            [
                "NAME:ComponentConfigurationData",
                [
                    "NAME:EnabledPorts"
                ],
                [
                    "NAME:EnabledMultipleComponents"
                ],
                [
                    "NAME:EnabledAnalyses",
                    [
                        "NAME:Port1",
                        "Port1:="		, ["LinearFrequency"]
                    ]
                ]
            ]
        ])
    oDesign.ChangePortProperty("Port1", 
        [
            "NAME:Port1",
            "IIPortName:="		, "Port1",
            "SymbolType:="		, 1,
            "DoPostProcess:="	, False
        ], 
        [
            [
                "NAME:Properties",
                [
                    "NAME:ChangedProps",
                    [
                        "NAME:rz",
                        "Value:="		, "50ohm"
                    ],
                    [
                        "NAME:iz",
                        "Value:="		, "0ohm"
                    ],
                    [
                        "NAME:pnum",
                        "Value:="		, "1"
                    ],
                    [
                        "NAME:EnableNoise",
                        "Value:="		, False
                    ],
                    [
                        "NAME:noisetemp",
                        "Value:="		, "16.85cel"
                    ]
                ]
            ]
        ])

    oDesign.UpdateSources(
        [
            "NAME:NexximSources",
            [
                "NAME:NexximSources",
                [
                    "NAME:Data",
                    [
                        "NAME:VoltageSinusoidal1",
                        "DataId:="		, "Source0",
                        "Type:="		, 1,
                        "Output:="		, 0,
                        "NumPins:="		, 2,
                        "Netlist:="		, "V@ID %0 %1 *DC(DC=@DC) SIN(?VO(@VO) ?VA(@VA) ?FREQ(@FREQ) ?TD(@TD) ?ALPHA(@ALPHA) ?THETA(@THETA)) *TONE(TONE=@TONE) *ACMAG(AC @ACMAG @ACPHASE)",
                        "CompName:="		, "Nexxim Circuit Elements\\Independent Sources:V_SIN",
                        "FDSFileName:="		, "",
                        "BtnPropFileName:="	, "",
                        [
                            "NAME:Properties",
                            "TextProp:="		, ["LabelID","HD","Property string for netlist ID","V@ID"],
                            "ValueProp:="		, ["ACMAG","OD","AC magnitude for small-signal analysis (Volts)","3.1*sqrt(2) V",0],
                            "ValuePropNU:="		, ["ACPHASE","D","AC phase for small-signal analysis","0deg",0,"deg",							"AdditionalPropInfo:="	, ""],
                            "ValueProp:="		, ["DC","D","DC voltage (Volts)","0V",0],
                            "ValueProp:="		, ["VO","D","Voltage offset from zero (Volts)","0V",0],
                            "ValueProp:="		, ["VA","D","Voltage amplitude (Volts)","0V",0],
                            "ValueProp:="		, ["FREQ","OD","Frequency (Hz)","10MHz",0],
                            "ValueProp:="		, ["TD","D","Delay to start of sine wave (seconds)","0s",0],
                            "ValueProp:="		, ["ALPHA","D","Damping factor (1/seconds)","0",0],
                            "ValuePropNU:="		, ["THETA","D","Phase delay","0deg",0,"deg",							"AdditionalPropInfo:="	, ""],
                            "ValueProp:="		, ["TONE","D","Frequency (Hz) to use for harmonic balance analysis, should be a submultiple of (or equal to) the driving frequency and should also be included in the HB analysis setup","0Hz",0],
                            "TextProp:="		, ["ModelName","SHD","","V_SIN"],
                            "ButtonProp:="		, ["CosimDefinition","D","","Edit","Edit",40501,							"ButtonPropClientData:=", []],
                            "MenuProp:="		, ["CoSimulator","D","","DefaultNetlist",0]
                        ]
                    ]
                ]
            ]
        ], 
        [
            "NAME:ComponentConfigurationData",
            [
                "NAME:ComponentConfigurationData",
                [
                    "NAME:EnabledPorts",
                    "VoltageSinusoidal1:="	, ["Port1"]
                ],
                [
                    "NAME:EnabledMultipleComponents",
                    "VoltageSinusoidal1:="	, []
                ],
                [
                    "NAME:EnabledAnalyses",
                    [
                        "NAME:Port1",
                        "Port1:="		, ["LinearFrequency"]
                    ],
                    [
                        "NAME:VoltageSinusoidal1",
                        "Port1:="		, ["LinearFrequency"]
                    ]
                ]
            ]
        ])
    oDesign.ChangePortProperty("Port1", 
        [
            "NAME:Port1",
            "IIPortName:="		, "Port1",
            "SymbolType:="		, 1,
            "DoPostProcess:="	, False
        ], 
        [
            [
                "NAME:Properties",
                [
                    "NAME:ChangedProps",
                    [
                        "NAME:rz",
                        "Value:="		, "1e-06ohm"
                    ],
                    [
                        "NAME:iz",
                        "Value:="		, "0ohm"
                    ],
                    [
                        "NAME:pnum",
                        "Value:="		, "1"
                    ],
                    [
                        "NAME:EnableNoise",
                        "Value:="		, False
                    ],
                    [
                        "NAME:noisetemp",
                        "Value:="		, "16.85cel"
                    ]
                ]
            ]
        ])
    oEditor.CreateComponent(
        [
            "NAME:ComponentProps",
            "Name:="		, "Nexxim Circuit Elements\\Probes:IPROBE",
            "Id:="			, "23"
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1,
            "X:="			, 0.01524,
            "Y:="			, 0.00254,
            "Angle:="		, 0,
            "Flip:="		, False
        ])
    oEditor.Move(
        [
            "NAME:Selections",
            "Selections:="		, ["CompInst@IPROBE;23;12"]
        ], 
        [
            "NAME:MoveParameters",
            "xdelta:="		, -0.00254,
            "ydelta:="		, 0.00254,
            "Disconnect:="		, False,
            "Rubberband:="		, False
        ])
    oEditor.Rotate(
        [
            "NAME:Selections",
            "Selections:="		, ["CompInst@IPROBE;23;12"]
        ], 
        [
            "NAME:RotateParameters",
            "Degrees:="		, 90,
            "Disconnect:="		, False,
            "Rubberband:="		, False
        ])
    oEditor.Rotate(
        [
            "NAME:Selections",
            "Selections:="		, ["CompInst@IPROBE;23;12"]
        ], 
        [
            "NAME:RotateParameters",
            "Degrees:="		, 90,
            "Disconnect:="		, False,
            "Rubberband:="		, False
        ])
    oEditor.Rotate(
        [
            "NAME:Selections",
            "Selections:="		, ["CompInst@IPROBE;23;12"]
        ], 
        [
            "NAME:RotateParameters",
            "Degrees:="		, 90,
            "Disconnect:="		, False,
            "Rubberband:="		, False
        ])
    oEditor.ChangeProperty(
        [
            "NAME:AllTabs",
            [
                "NAME:PassedParameterTab",
                [
                    "NAME:PropServers", 
                    "CompInst@IPROBE;23;12:1"
                ],
                [
                    "NAME:ChangedProps",
                    [
                        "NAME:Name",
                        "Value:="		, "Iin"
                    ]
                ]
            ]
        ])
    oEditor.CreateComponent(
        [
            "NAME:ComponentProps",
            "Name:="		, "Nexxim Circuit Elements\\Probes:IPROBE",
            "Id:="			, "24"
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1,
            "X:="			, -0.03302,
            "Y:="			, -0.00254,
            "Angle:="		, 0,
            "Flip:="		, False
        ])
    oEditor.Rotate(
        [
            "NAME:Selections",
            "Selections:="		, ["CompInst@IPROBE;24;19"]
        ], 
        [
            "NAME:RotateParameters",
            "Degrees:="		, 90,
            "Disconnect:="		, False,
            "Rubberband:="		, False
        ])
    oEditor.Rotate(
        [
            "NAME:Selections",
            "Selections:="		, ["CompInst@IPROBE;24;19"]
        ], 
        [
            "NAME:RotateParameters",
            "Degrees:="		, 90,
            "Disconnect:="		, False,
            "Rubberband:="		, False
        ])
    oEditor.ChangeProperty(
        [
            "NAME:AllTabs",
            [
                "NAME:PassedParameterTab",
                [
                    "NAME:PropServers", 
                    "CompInst@IPROBE;24;19:1"
                ],
                [
                    "NAME:ChangedProps",
                    [
                        "NAME:Name",
                        "Value:="		, "Iout"
                    ]
                ]
            ]
        ])
    oEditor.CreateComponent(
        [
            "NAME:ComponentProps",
            "Name:="		, "Nexxim Circuit Elements\\Resistors:RES_",
            "Id:="			, "25"
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1,
            "X:="			, -0.0508,
            "Y:="			, -0.00254,
            "Angle:="		, 0,
            "Flip:="		, False
        ])
    oEditor.CreateWire(
        [
            "NAME:WireData",
            "Name:="		, "",
            "Id:="			, 26,
            "Points:="		, ["(-0.045720, -0.002540)","(-0.038100, -0.002540)"]
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1
        ])
    oEditor.CreateWire(
        [
            "NAME:WireData",
            "Name:="		, "",
            "Id:="			, 32,
            "Points:="		, ["(-0.027940, -0.002540)","(-0.010160, -0.002540)"]
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1
        ])
    oEditor.CreateWire(
        [
            "NAME:WireData",
            "Name:="		, "",
            "Id:="			, 38,
            "Points:="		, ["(0.000000, -0.002540)","(0.012700, -0.002540)","(0.012700, 0.000000)"]
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1
        ])
    oEditor.CreateGround(
        [
            "NAME:GroundProps",
            "Id:="			, 44
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1,
            "X:="			, -0.05588,
            "Y:="			, -0.01778,
            "Angle:="		, 0,
            "Flip:="		, False
        ])
    oEditor.CreateWire(
        [
            "NAME:WireData",
            "Name:="		, "",
            "Id:="			, 48,
            "Points:="		, ["(-0.055880, -0.015240)","(-0.055880, -0.002540)"]
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1
        ])
    oEditor.ChangeProperty(
        [
            "NAME:AllTabs",
            [
                "NAME:PassedParameterTab",
                [
                    "NAME:PropServers", 
                    "CompInst@RES_;25;24:1"
                ],
                [
                    "NAME:ChangedProps",
                    [
                        "NAME:R",
                        "Value:="		, "28"
                    ]
                ]
            ]
        ])
    oEditor.CreateWire(
        [
            "NAME:WireData",
            "Name:="		, "",
            "Id:="			, 53,
            "Points:="		, ["(0.012700, 0.010160)","(0.012700, 0.012700)"]
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1
        ])
    oEditor.CreateComponent(
        [
            "NAME:ComponentProps",
            "Name:="		, "Nexxim Circuit Elements\\Probes:VPROBE",
            "Id:="			, "26"
        ], 
        [
            "NAME:Attributes",
            "Page:="		, 1,
            "X:="			, 0.0254,
            "Y:="			, -0.00508,
            "Angle:="		, 0,
            "Flip:="		, False
        ])
    oEditor.Move(
        [
            "NAME:Selections",
            "Selections:="		, ["CompInst@VPROBE;26;57"]
        ], 
        [
            "NAME:MoveParameters",
            "xdelta:="		, -0.06604,
            "ydelta:="		, 0.00762,
            "Disconnect:="		, False,
            "Rubberband:="		, False
        ])
    oEditor.ChangeProperty(
        [
            "NAME:AllTabs",
            [
                "NAME:PassedParameterTab",
                [
                    "NAME:PropServers", 
                    "CompInst@VPROBE;26;57:1"
                ],
                [
                    "NAME:ChangedProps",
                    [
                        "NAME:Name",
                        "Value:="		, "Vout"
                    ]
                ]
            ]
        ])



def create_output_variables(circuit_design=None):

    oModule = circuit_design.GetModule("OutputVariable")
    oModule.CreateOutputVariable("Pin", "0.5*re(V(Port1)*conjg(I(Iin)))", "LinearFrequency", "Standard", 
        [
            "NAME:Context",
            "SimValueContext:="	, [3,0,2,0,False,False,-1,1,0,1,1,"",0,0]
        ])
    oModule.CreateOutputVariable("Pout", "0.5*re(V(Vout)*conjg(I(Iout)))", "LinearFrequency", "Standard", 
        [
            "NAME:Context",
            "SimValueContext:="	, [3,0,2,0,False,False,-1,1,0,1,1,"",0,0]
        ])
    oModule.CreateOutputVariable("Gv", "mag(V(Vout))/mag(V(Port1))", "LinearFrequency", "Standard", 
        [
            "NAME:Context",
            "SimValueContext:="	, [3,0,2,0,False,False,-1,1,0,1,1,"",0,0]
        ])
    oModule.CreateOutputVariable("Eff", "Pout/Pin*100", "LinearFrequency", "Standard", 
        [
            "NAME:Context",
            "SimValueContext:="	, [3,0,2,0,False,False,-1,1,0,1,1,"",0,0]
        ])



def create_report(project, circuit_design=None, name=""):
    
    oModule = circuit_design.GetModule("ReportSetup")
    oModule.CreateReport(f"Return Loss Table {name}", "Standard", "Data Table", "LinearFrequency", 
        [
            "NAME:Context",
            "SimValueContext:="	, [3,0,2,0,False,False,-1,1,0,1,1,"",0,0]
        ], 
        [
            "Freq:="		, ["All"]
        ], 
        [
            "X Component:="		, "Freq",
            "Y Component:="		, ["Pin","Pout","Gv","Eff","mag(V(Port1))","mag(V(Vout))","mag(I(Iin))","mag(I(Iout))"]
        ])



    project_dir = project.path

    sim_data_RL = circuit_design.post.export_report_to_csv(project_dir=project_dir, plot_name=f"Return Loss Table {name}", uniform=False, start=None, end=None, step=None, use_trace_number_format=False)

    data_RL = pd.read_csv(sim_data_RL)
    data_RL = circuit_design.post_processing.data_preprocessing(data_RL)

    # 단위 변환 및 컬럼명 변경
    rename_dict = {}
    for col in data_RL.columns:
        if "[mV]" in col:
            data_RL[col] = data_RL[col] / 1000  # mV → V
            new_col = col.replace("[mV]", "[V]")
            rename_dict[col] = new_col
        elif "[mA]" in col:
            data_RL[col] = data_RL[col] / 1000  # mA → A
            new_col = col.replace("[mA]", "[A]")
            rename_dict[col] = new_col

    # 컬럼 이름 일괄 변경
    data_RL.rename(columns=rename_dict, inplace=True)

    data_Pin = round(data_RL["Pin []"].values[0],4)
    data_Pout = round(data_RL["Pout []"].values[0],4)
    data_Gv = round(data_RL["Gv []"].values[0],4)
    data_eff = round(data_RL["Eff []"].values[0],4)
    data_Vin = round(data_RL["mag(V(Port1)) [V]"].values[0],4)
    data_Vload = round(data_RL["mag(V(Vout)) [V]"].values[0],4)
    data_Iin = round(data_RL["mag(I(Iin)) [A]"].values[0],4)
    data_Iload = round(data_RL["mag(I(Iout)) [A]"].values[0],4)

    columns = [f'Pin_{name}', f'Pout_{name}', f'Gv_{name}', f'eff_{name}', f'Vin_{name}', f'Vload_{name}', f'Iin_{name}', f'Iload_{name}']
    circuit_data_raw = [data_Pin, data_Pout, data_Gv, data_eff, data_Vin, data_Vload, data_Iin, data_Iload]
    circuit_data_df = pd.DataFrame([circuit_data_raw], columns=columns)

    return circuit_data_df




def change_R(circuit_design=None, R=None):

    oEditor = circuit_design.SetActiveEditor("SchematicEditor")

    oEditor.ChangeProperty(
        [
            "NAME:AllTabs",
            [
                "NAME:PassedParameterTab",
                [
                    "NAME:PropServers", 
                    "CompInst@RES_;25;24"
                ],
                [
                    "NAME:ChangedProps",
                    [
                        "NAME:R",
                        "Value:="		, f"{R}"
                    ]
                ]
            ]
        ])
