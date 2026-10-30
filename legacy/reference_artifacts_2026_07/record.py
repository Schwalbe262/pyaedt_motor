# ----------------------------------------------
# Script Recorded by Ansys Electronics Desktop Version 2025.2.0
# 21:18:59  Jun 02, 2026
# ----------------------------------------------
import ScriptEnv
ScriptEnv.Initialize("Ansoft.ElectronicsDesktop")
oDesktop.RestoreWindow()
oProject = oDesktop.SetActiveProject("simulation30")
oDefinitionManager = oProject.GetDefinitionManager()
oDefinitionManager.AddMaterial(
	[
		"NAME:steel_1008 - Copy",
		"CoordinateSystemType:=", "Cartesian",
		"BulkOrSurfaceType:="	, 1,
		[
			"NAME:PhysicsTypes",
			"set:="			, ["Electromagnetic","Thermal","Structural"]
		],
		[
			"NAME:AttachedData",
			[
				"NAME:MatAppearanceData",
				"property_data:="	, "appearance_data",
				"Red:="			, 161,
				"Green:="		, 161,
				"Blue:="		, 161
			]
		],
		[
			"NAME:permeability",
			"property_type:="	, "nonlinear",
			"BTypeForSingleCurve:="	, "normal",
			"HUnit:="		, "A_per_meter",
			"BUnit:="		, "tesla",
			"IsTemperatureDependent:=", False,
			[
				"NAME:BHCoordinates",
				[
					"NAME:DimUnits", 
					"", 
					""
				],
				[
					"NAME:Point", 
					0, 
					0
				],
				[
					"NAME:Point", 
					159.2, 
					0.2402
				],
				[
					"NAME:Point", 
					318.3, 
					0.8654
				],
				[
					"NAME:Point", 
					477.5, 
					1.1106
				],
				[
					"NAME:Point", 
					636.6, 
					1.2458
				],
				[
					"NAME:Point", 
					795.8, 
					1.331
				],
				[
					"NAME:Point", 
					1591.5, 
					1.5
				],
				[
					"NAME:Point", 
					3183.1, 
					1.6
				],
				[
					"NAME:Point", 
					4774.6, 
					1.683
				],
				[
					"NAME:Point", 
					6366.2, 
					1.741
				],
				[
					"NAME:Point", 
					7957.7, 
					1.78
				],
				[
					"NAME:Point", 
					15915.5, 
					1.905
				],
				[
					"NAME:Point", 
					31831, 
					2.025
				],
				[
					"NAME:Point", 
					47746.5, 
					2.085
				],
				[
					"NAME:Point", 
					63662, 
					2.13
				],
				[
					"NAME:Point", 
					79577.5, 
					2.165
				],
				[
					"NAME:Point", 
					159155, 
					2.28
				],
				[
					"NAME:Point", 
					318310, 
					2.485
				],
				[
					"NAME:Point", 
					397887, 
					2.5851
				]
			],
			[
				"NAME:Temperatures"
			],
			"RecoilPermeability:="	, "1"
		],
		"conductivity:="	, "2000000",
		"magnetic_loss_tangent:=", "0",
		[
			"NAME:magnetic_coercivity",
			"property_type:="	, "VectorProperty",
			"Magnitude:="		, "0A_per_meter",
			"DirComp1:="		, "1",
			"DirComp2:="		, "0",
			"DirComp3:="		, "0"
		],
		"thermal_conductivity:=", "45",
		[
			"NAME:core_loss_type",
			"property_type:="	, "ChoiceProperty",
			"Choice:="		, "B-P Curve"
		],
		"core_loss_kdc:="	, "0",
		"mass_density:="	, "7000",
		"specific_heat:="	, "481",
		"youngs_modulus:="	, "200000000000",
		"poissons_ratio:="	, "0.25",
		"thermal_expansion_coefficient:=", "1.26e-05",
		"core_loss_equiv_cut_depth:=", "0.001meter",
		[
			"NAME:core_loss_curves",
			"property_type:="	, "nonlinear",
			"PUnit:="		, "w/kg",
			"BUnit:="		, "tesla",
			"Frequency:="		, "80Hz",
			"Thickness:="		, "0.3mm",
			"IsTemperatureDependent:=", False,
			[
				"NAME:BPCoordinates",
				[
					"NAME:DimUnits", 
					"", 
					""
				],
				[
					"NAME:Point", 
					0, 
					0
				],
				[
					"NAME:Point", 
					0.1, 
					0.016502097
				],
				[
					"NAME:Point", 
					0.109056278, 
					0.019664266
				],
				[
					"NAME:Point", 
					0.136628796, 
					0.029541207
				],
				[
					"NAME:Point", 
					0.158599299, 
					0.039649034
				],
				[
					"NAME:Point", 
					0.177827941, 
					0.048749186
				],
				[
					"NAME:Point", 
					0.197324393, 
					0.061074504
				],
				[
					"NAME:Point", 
					0.214449627, 
					0.070094384
				],
				[
					"NAME:Point", 
					0.232254379, 
					0.079445235
				],
				[
					"NAME:Point", 
					0.249798823, 
					0.090043587
				],
				[
					"NAME:Point", 
					0.296063985, 
					0.120098527
				],
				[
					"NAME:Point", 
					0.396179107, 
					0.190880464
				],
				[
					"NAME:Point", 
					0.49806865, 
					0.274458722
				],
				[
					"NAME:Point", 
					0.594424053, 
					0.363783301
				],
				[
					"NAME:Point", 
					0.69722592, 
					0.467317801
				],
				[
					"NAME:Point", 
					0.800966699, 
					0.581816094
				],
				[
					"NAME:Point", 
					0.901196109, 
					0.706451173
				],
				[
					"NAME:Point", 
					0.96591688, 
					0.7956968
				],
				[
					"NAME:Point", 
					1.031701866, 
					0.890622989
				]
			],
			[
				"NAME:Temperatures"
			]
		]
	])
oDesign = oProject.SetActiveDesign("IPMSM")
oEditor = oDesign.SetActiveEditor("3D Modeler")
oEditor.ChangeProperty(
	[
		"NAME:AllTabs",
		[
			"NAME:Geometry3DAttributeTab",
			[
				"NAME:PropServers", 
				"stator"
			],
			[
				"NAME:ChangedProps",
				[
					"NAME:Material",
					"Value:="		, "\"TDK_FB12B_60cel\""
				]
			]
		]
	])
