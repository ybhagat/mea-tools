.PHONY: inline ui clean

OS := $(shell uname)

ifneq (,$(findstring NT-5.1,$(OS)))
DFLAGS=-DMS_WIN64
endif

inline:
	python3 setup.py build_ext $(DFLAGS) --inplace

ui:
	pyside-uic pymea/ui/PyMEAMainWindow.ui -o pymea/ui/main_window.py

clean:
	-@rm -rf pymea/mea_cython.c pymea/mea_cython.so pymea/mea_cython.pyd 2>/dev/null 
