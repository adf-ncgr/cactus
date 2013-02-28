# order is important, libraries first
modules = api setup blast caf bar normalisation reference phylogeny faces check pipeline progressive preprocessor hal dbTest

.PHONY: all %.all clean %.clean

all : ${modules:%=all.%}

all.%:
	cd $* && make all

clean:  ${modules:%=clean.%}
	rm -rf lib/*.h bin/*.dSYM

clean.%:
	cd $* && make clean
	
test: all
	python allTests.py