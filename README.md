📌 **Project Title:** **THE PROJECTING AN AUTONOMOUS ROBOT OF THE RESCUE MAZE CATEGORY**  
📅 **Project Timeline:** **August 2019 – October 2021**  
🎥 YouTube Demo: [Link: https://youtu.be/3sTD7d_HzC4](https://youtu.be/3sTD7d_HzC4)  
📦 GitHub Source Code: <https://github.com/IvanSicaja/2019.08.01_GitHub_The-Projecting-an-Autonomous-Robot-of-the-Rescue-Maze-Category>  

---

📍 My Personal Profiles ⬇︎  
🎥 Video Portfolio: To be added  
📦 GitHub Profile: <https://github.com/IvanSicaja>  
👔 LinkedIn: <https://www.linkedin.com/in/ivan-si%C4%8Daja-832682222>  
🎥 YouTube: <https://www.youtube.com/@ivan_sicaja>  

---

### 💡 Core Challenge This Project Resolves:

Designing and engineering a fully integrated autonomous rescue robot capable of real-time perception, decision-making, navigation, victim detection, and mechanical adaptability in unpredictable maze environments under hardware and computational constraints.

---

### 🔧 Core Skills Tree Used To Build The Project - Skills and Tech Stack:
*(Project-Specific Structured Overview)*
```
│
├── Software Engineering
│ ├── Software / Frameworks / Libraries
│ │ ├── Python
│ │ ├── C++ (Arduino firmware development)
│ │ ├── TensorFlow
│ │ ├── Keras
│ │ ├── OpenCV
│ │ ├── scikit-learn
│ │ ├── Pandas
│ │ ├── Git / GitHub
│ │ ├── Linux
│ │ ├── Visual Studio Code
│ │ └── Turtle (Python – maze visualization & mapping)
│ │
│ ├── Hardware
│ │ ├── Raspberry Pi 4B
│ │ ├── Teensy 3.5
│ │ └── Arduino
│ │
│ └── Skills
│   ├── Embedded software development & firmware programming
│   ├── Real-time sensor data acquisition & processing
│   ├── Computer vision pipeline architecture
│   ├── AI model training, validation & optimization
│   ├── Implementation of search algorithms (BFS, DFS, A*)
│   ├── Linux-based robotics workflow management
│   ├── System-level debugging & integration
│   ├── Performance optimization under limited computational resources
│   └── Multi-controller distributed architecture design
│
├── Mechanical Engineering
│ ├── Software / Frameworks / Libraries
│ │ └── Autodesk Fusion 360 (CAD/CAM design & simulation)
│ │
│ ├── Hardware / Hardware Tools
│ │ └── Ultimaker 3+ (3D printing system)
│ │
│ └── Skills
│   ├── Full robot chassis design & assembly modeling
│   ├── Drivetrain engineering & torque optimization
│   ├── 25-degree incline climbing capability design
│   ├── Independent axle maneuvering mechanism
│   ├── Structural strength & grip optimization
│   ├── 3D printing parameter optimization (density, material selection)
│   ├── Prototype validation & mechanical stress evaluation
│   └── Mechanical-electrical integration alignment
│
├── Electrical Engineering
│ ├── Software / Frameworks / Libraries
│ │ └── Arduino IDE
│ │
│ ├── Hardware Components
│ │ ├── Teensy 3.5
│ │ ├── Raspberry Pi 4B
│ │ ├── Arduino boards
│ │ ├── Optical cameras (2x)
│ │ ├── Thermal cameras (2x)
│ │ ├── IR LiDAR sensors (6x)
│ │ ├── Color sensor
│ │ ├── Wheel encoders
│ │ └── Motors & motor drivers
│ │
│ ├── Hardware Tools
│ │ └── Power supply
│ │
│ ├── Communication Protocols
│ │ ├── UART / Serial
│ │ ├── I2C
│ │ ├── SPI
│ │ └── USB
│ │
│ └── Skills
│   ├── Sensor calibration & integration
│   ├── Signal filtering & noise reduction
│   ├── Encoder-based position tracking systems
│   ├── Multi-board communication architecture
│   ├── Electrical system wiring & validation
│   ├── Hardware troubleshooting & diagnostics
│   ├── Power management & distribution optimization
│   └── Embedded hardware-software synchronization
│
├── Data Science & Artificial Intelligence
│ ├── Software / Frameworks / Libraries
│ │ ├── TensorFlow
│ │ ├── Keras
│ │ ├── OpenCV
│ │ ├── Pandas
│ │ └── scikit-learn
│ │
│ ├── Hardware
│ │ └── (Camera systems & sensors integrated via Raspberry Pi 4B)
│ │
│ └── Skills
│   ├── Convolutional Neural Network (CNN) architecture design
│   ├── OCR model training & evaluation (77.54% accuracy target)
│   ├── Image preprocessing (grayscale, Gaussian blur, threshold, dilation)
│   ├── Edge detection & dynamic noise filtering
│   ├── Custom object detection scripting
│   ├── Maze mapping & graph representation
│   ├── Shortest-path computation using BFS, DFS, A*
│   ├── Data transformation for performance acceleration (.CSV optimization)
│   └── Overfitting prevention & model generalization strategies
│
└── Research & Development Engineering
  ├── Software / Frameworks / Libraries
  │ └── Integrated within sections above
  │
  ├── Hardware / Hardware Tools
  │ └── Integrated within sections above
  │
  └── Skills
    ├── System architecture design from concept to prototype
    ├── Hardware feasibility analysis & component selection
    ├── Iterative testing & calibration cycles
    ├── Cross-disciplinary engineering coordination
    ├── Technical documentation & publication preparation
    ├── Experimental validation & benchmarking
    └── End-to-end robotics system development
```

---

### 📋 Core System Capabilities - List Only:

- **Autonomous character recognition (OCR)**
- **Autonomous color recognition**
- **Partially autonomous drive in the maze** (need a lot of testing and calibration for fully autonomous drive and labyrinth mapping)
- **Thermal victim recognition**
- **Package delivery**
- **Ability to master a climb of 25 degrees** (all-wheel drive, strong grip)
- **Independent axle maneuvering**
- **Remembering positions (encoders)**...

---

### 🧠️ How It Works - Core System Capabilities Workflow:

The project is very complex and demands knowledge in different areas (**3D modeling, 3D printing, advanced programming skills in different languages, researching ability, expert knowledge of every electrical component working principles, image processing, cause-and-effect analysis...**)  
The brains of the robot are **microcontroller Teensy 3.5** and **Raspberry Pi 4B**.

The robot is also equipped with:

- **2x optical camera**
- **2x thermal camera**
- **6x IR lidar sensor**
- **1x color sensor**... (more can be found at GitHub in my publication paper: _The projecting an autonomous robot of the rescue maze category.pdf_ -> Caption 4.3)

**Optical character recognition:**  
In this project, we trained a **Convolutional Neural Network (CNN)** on an image examples with the Python module **TensorFlow**. Images are converted into **.CSV file** because of speeder processing. The input image is filtered with different filters (**Grayscale, Gaussian Blur, Threshold, Binary, Dilatation**) in order to speed up image processing (replace three color channels with one channel, **RGB -> grayscale**). Reduce noises (the dust on the live video capturing). Getting smooth and sharp character edges is the most important characteristic for successful character recognition. The trained model accuracy is **77.54%** which is a target because we want to get high reliability and avoid CNN overfitting.

**Object detection:**  
Developed the custom script with the Python computer vision module **OpenCV** which filters the character that should be recognized from the other objects in the robot's surroundings. The script works on the principle of **character height and proportion**, together with the **dynamic noise filtering**.

**Maze mapping:**  
Maze mapping is done in my Python module **Turtle**. Every maze field is properly recognized by the robot's **distance and color sensors**. After all maze fields are mapped, they are sent to the backend and the shortest path is calculated by the artificial intelligence searching algorithms such as: _Breadth-First Search, Depth-First Search, A Algorithm_\*...

**Hardware choosing and connecting:**  
The entire process of choosing **hardware platforms, supported protocols, and hardware capabilities** is done. E.g. the **video camera** must have a corresponding **focal length** otherwise it will be useless, **framerate, resolution, additional light source**, **motors** should have expected speed, **distance sensors** should be precise and able to work in a maze, **robot brain** should be able to do high computation payload and support Python…

**Frame design and 3D printing:**  
Entire robot is **3D designed** with **Fusion 360 CAD/CAM** software and **3D printed** with the **Ultimaker 3+** 3D printer with corresponding **filament, density**, etc.

**Developing mechatronic code:**  
**Arduino** is used to control all **sensors and actuators** on the robot except the **camera** which is controlled by **Raspberry Pi 4B** computer.

---

### ⚠️ Note:

Achieving fully **autonomous drive** and **labyrinth mapping** requires extensive testing and calibration.  
I would especially like to thank **Mirko Pezo** and **Stjepan Mikulic** for their exceptional contribution to the development of this project .

---

### 📸 Project Snapshots:

<p align="center">
  <img src="https://github.com/IvanSicaja/2019.08.01_GitHub_The-Projecting-an-Autonomous-Robot-of-the-Rescue-Maze-Category/blob/main/publish/2.0_Thumbnail_1.png?raw=true" 
       alt="Rescue Maze Robot Preview 1" 
       width="640" 
       height="360">
</p>

<p align="center">
  <img src="https://github.com/IvanSicaja/2019.08.01_GitHub_The-Projecting-an-Autonomous-Robot-of-the-Rescue-Maze-Category/blob/main/publish/2.0_Thumbnail_2.png?raw=true" 
       alt="Rescue Maze Robot Preview 2" 
       width="640" 
       height="360">
</p>

<p align="center">
  <img src="https://github.com/IvanSicaja/2019.08.01_GitHub_The-Projecting-an-Autonomous-Robot-of-the-Rescue-Maze-Category/blob/main/publish/2.0_Thumbnail_3.png?raw=true" 
       alt="Rescue Maze Robot Preview 3" 
       width="640" 
       height="360">
</p>

<p align="center">
  <img src="https://github.com/IvanSicaja/2019.08.01_GitHub_The-Projecting-an-Autonomous-Robot-of-the-Rescue-Maze-Category/blob/main/publish/2.0_Thumbnail_4.png?raw=true" 
       alt="Rescue Maze Robot Preview 4" 
       width="640" 
       height="360">
</p>

<p align="center">
  <img src="https://github.com/IvanSicaja/2019.08.01_GitHub_The-Projecting-an-Autonomous-Robot-of-the-Rescue-Maze-Category/blob/main/publish/2.0_Thumbnail_5.png?raw=true" 
       alt="Rescue Maze Robot Preview 5" 
       width="640" 
       height="360">
</p>

<p align="center">
  <img src="https://github.com/IvanSicaja/2019.08.01_GitHub_The-Projecting-an-Autonomous-Robot-of-the-Rescue-Maze-Category/blob/main/publish/2.0_Thumbnail_6.png?raw=true" 
       alt="Rescue Maze Robot Preview 6" 
       width="640" 
       height="360">
</p>

<p align="center">
  <img src="https://github.com/IvanSicaja/2019.08.01_GitHub_The-Projecting-an-Autonomous-Robot-of-the-Rescue-Maze-Category/blob/main/publish/2.0_Thumbnail_7.png?raw=true" 
       alt="Rescue Maze Robot Preview 7" 
       width="640" 
       height="360">
</p>

---

### 🎥 Video Demonstration:

<p align="center">
  <a href="https://youtu.be/3sTD7d_HzC4">
    <img src="https://img.youtube.com/vi/3sTD7d_HzC4/0.jpg" 
         alt="Watch the demo" 
         width="640" 
         height="1000">
  </a>
</p>

---

### 📣 Hashtags Section:

**#AutonomousRobotics #RescueMaze #RoboticsEngineering #AI #ComputerVision #OCR #ObjectDetection #PathPlanning #CNN #TensorFlow #OpenCV #EmbeddedSystems #RaspberryPi #Arduino #3DPrinting #Fusion360 #Mechatronics #AutonomousNavigation #MachineLearning**
