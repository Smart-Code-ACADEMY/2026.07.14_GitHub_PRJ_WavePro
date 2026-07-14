1. Make sure that venv you are using is not moved, renamed or corrupted. If so delete venv, create new one, delete .idea project folder, 
   and set up this new installed interpreter for your project in pycharm.
   1. remove old .venv from available interpreters in pycharm                                                                                                                       
   2. configure you interpreted with that new .venv                                                                                                                                 
   3. open your project terminal                                                                                                                                                    
   4. press "x" on local                                                                                                                                                            
   5. make sure now stays e.g. "(.venv) PS C:\Users\Sicaja\Desktop\DB\06_DB-bussiness-and-bussiness-assets\2019.01.01_SmartCodeACADEMY_Official\2025.11.24_GitHub_PRJ_Scenify>      

2. Install PyInstaller in your venv, otherwise it will use interpreter which already has it installed and it will make you a lot of troubles.
pip install pyinstaller

3. Navigate to the folder where your main.py file is located:
cd "C:\User..."
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
4. Create a standalone executable with icon
pyinstaller --onefile --windowed --name InstaFlow --icon=..\assets\01_media\01_icons\icon.ico --distpath publish --workpath publish\build --specpath publish main\main.py

5. Optional for removing non necessary files -> run in pycharm terminal
Get-ChildItem -Path .\publish -Recurse | Where-Object { $_.Extension -ne '.exe' } | Remove-Item -Force -Recurse