import sys, subprocess
path = sys.argv[1] if len(sys.argv) > 1 else ''
subprocess.Popen(['C:\\users\\attelnd\\Work Folders\\Desktop\\ParaView-6.1.0-Windows-Python3.12-msvc2017-AMD64\\bin\\paraview.exe', path])
