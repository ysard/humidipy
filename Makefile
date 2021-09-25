upload:
	ampy -p /dev/ttyUSB0 put src/main.py

upload_bytecode: compile
	ampy -p /dev/ttyUSB0 put main.mpy

compile:
	../micropython/mpy-cross/mpy-cross -march=xtensa -o main.mpy src/main.py
