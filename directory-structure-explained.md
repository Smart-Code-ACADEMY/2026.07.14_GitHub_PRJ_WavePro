project-name/
│
│
├── assets/                       # Additional project resources (non-code)
│   │
│   ├── 01_media/                 # Organized media resources
│   │   ├── 01_icons/             # Icons used in UI, documentation, branding
│   │   ├── 02_figures/           # Diagrams, charts, illustrations
│   │   ├── 03_sounds/            # Audio assets (alerts, UI sounds, background)
│   │   ├── 04_videos/            # Demo videos, screen recordings, previews
│   │   └── 05_ads/               # Final marketing visuals & promotional materials (thumbnails, banners, ad creatives, campaign text, landing visuals, social media posts, etc.)
│   │
│   └── 02_3d-modeling/           # 3D objects (STL, GLB, OBJ, etc.)
│                                  # CAD files, simulation models, renders
│
├── bills/                        # Financial records related to this project
│                                 # Invoices, receipts, subscriptions, tools paid,
│                                 # hardware purchases, service costs
│
├── copyright/                    # Legal & attribution information
│                                 # Licenses (MIT, GPL, etc.), third-party attributions,
│                                 # authorship records, usage rights documentation
│
├── data/                         # Small datasets (<100MB; larger hosted externally)
│   │
│   ├── raw/                      # Original unprocessed data (CSV, TXT, SQL, XLSX)
│   └── processed/                # Cleaned, transformed, ready-to-use datasets
│
├── docs/                         # Documentation & written materials
│   │
│   ├── 01_structure-trees/                       # System architecture trees, folder maps, logic diagrams, structural blueprints 
│   ├── 02_cores-docs-and-comparison-tables/      # Key feature comparison for the specific topic 
│   ├── 03_req-files/                             # Environment dependency files 
│   ├── 04_problems-to-solve/                     # List of problems which needs to be solved
│   └── (other documentation files)               # Reports, whitepapers, research explanations    
│                                  
│
├── main/                         # Main source code (production-ready)
│   └── main.py                   # Project entry point
│
├── models/                       # Machine learning / AI models (<100MB locally)
│   │
│   ├── 01_downloaded/            # Pre-trained or externally sourced models
│   └── 02_trained/               # Models trained within this project
│
├── notebooks/                    # Jupyter / Colab notebooks
│                                  # Experiments, EDA, prototypes (archived snapshots)
│
├── publish/                      # Final deliverables
│   ├── main.exe                  # Executable release
│   └── (other release builds)    # Installers, packaged builds, exports
│
├── tests/                        # Integration & validation tests
│                                  # Unit tests, system tests, validation scripts
│
├── viz/                          # Visualization outputs & dashboards
│   │
│   │
│   ├── 01_mind-maps/             # Mind maps, conceptual maps, system diagrams (XMind, Miro exports, etc.)
│   └── 02_tableau/               # Tableau workbooks (.twb, .twbx)
│                         
│
├── .gitignore                         # Ignored files & directories
├── requirements.txt                   # Python dependencies
├── directory_structure_explained.md   # Explanation which files can be found in the which folder
└── README.md                          # Project overview & documentation entry
