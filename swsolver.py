
from snowwhite import *
import snowwhite as sw
from snowwhite.metadata import *

import datetime
import subprocess
import os
import sys
import json

import tempfile
import shutil

import numpy as np

try:
    import cupy as cp
except ModuleNotFoundError:
    cp = None

import ctypes
import sys



class SWProblem:
    """Base class for SnowWhite problem."""
    
    def __init__(self):
        pass
        

class SWSolver:
    """Base class for SnowWhite solver."""
    
    def __init__(self, problem: SWProblem, namebase = 'func', opts = {}):
        self._problem = problem
        self._opts = opts
        self._colMajor = self._opts.get(SW_OPT_COLMAJOR, False)
        self._genHIP = (self._opts.get(SW_OPT_PLATFORM, SW_CPU) == SW_HIP)
        self._genCuda = (self._opts.get(SW_OPT_PLATFORM, SW_CPU) == SW_CUDA)
        self._keeptemp = self._opts.get(SW_OPT_KEEPTEMP, False)
        self._withMPI = self._opts.get(SW_OPT_MPI, False)
        self._printRuleTree = self._opts.get(SW_OPT_PRINTRULETREE, False)
        self._runResult = None
        self._tracingOn = False
        self._callGraph = []
        self._SharedLibAccess = None
        self._MainFunc = None
        self._spiralname = 'spiral'
        self._metadata = dict()
        self._includeMetadata = self._opts.get(SW_OPT_METADATA, False)
        
        # find and possibly create the subdirectory of temp dirs
        moduleDir = os.path.dirname(os.path.realpath(__file__))
        self._libsDir = os.path.join(moduleDir, '.libs')
        os.makedirs(self._libsDir, mode=0o777, exist_ok=True)
        
        if self._genCuda:
            self._namebase = namebase + '_cu'
        elif self._genHIP:
            self._namebase = namebase + '_hip'
        else:
            self._namebase = namebase
        if sys.platform == 'win32':
            libext = '.dll'
        else:
            libext = '.so'
        sharedLibFullPath = os.path.join(self._libsDir, 'lib' + self._namebase + libext)         

        if not os.path.exists(sharedLibFullPath):
            self._setupCFuncs(self._namebase)

        self._SharedLibAccess = ctypes.CDLL (sharedLibFullPath)
        ##  print ( 'SWSolver.__init__: Find main function in library, _namebase = ' + self._namebase, flush = True )
        self._MainFunc = getattr(self._SharedLibAccess, self._namebase)
        if self._MainFunc == None:
            msg = 'could not find function: ' + self._namebase
            raise RuntimeError(msg)
            
        self._initFunc()

    def __del__(self):
        self._destroyFunc()
    
    def solve(self):
        raise NotImplementedError()

    def runDef(self):
        raise NotImplementedError()
        
    def _writeScript(self, script_file):
        raise NotImplementedError()
    
    def _genScript(self, filename : str):
        print("Tracing Python description to generate SPIRAL script");
        self._trace()
        try:
            script_file = open(filename, 'w')
        except:
            print('Error: Could not open ' + filename + ' for writing')
            return
        timestr = datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y")
        print(file = script_file)
        print("# SPIRAL script generated by " + type(self).__name__, file = script_file)
        print('# ' + timestr, file = script_file)
        print(file = script_file)
        self._writeScript(script_file)
        script_file.close()
        
    def _functionMetadata(self):
        return None
        
    def _buildMetadata(self):
        md = self._metadata
        md[SW_KEY_SPIRALBUILDINFO] = spiralBuildInfo()
        funcmeta = self._functionMetadata()
        if type(funcmeta) is dict:
            md[SW_KEY_TRANSFORMS] = [ funcmeta ]
            md[SW_KEY_TRANSFORMTYPES] = [ funcmeta.get(SW_KEY_TRANSFORMTYPE, SW_TRANSFORM_UNKNOWN) ]
    
    def _createMetadataFile(self, basename):
        """Write metadata source file."""
        varname  = basename + SW_METAVAR_EXT
        filename = basename + SW_METAFILE_EXT
        self._buildMetadata()
        writeMetadataSourceFile(self._metadata, varname, filename)    
        
    def _callSpiral(self, script):
        """Run SPIRAL with script as input."""
        if self._genCuda:
            print ( 'Generating CUDA code', flush = True )
        elif self._genHIP:
            print ( 'Generating HIP code', flush = True )
        else:
            print ( 'Generating C code' )
        if sys.platform == 'win32':
            spiralexe = self._spiralname + '.bat'
            self._runResult = subprocess.run([spiralexe,'<',script], shell=True, capture_output=True)
        else:
            spiralexe = self._spiralname
            cmd = spiralexe + ' < ' + script
            self._runResult = subprocess.run(cmd, shell=True)

    def _callCMake (self, basename):
        ##  create a temporary work directory in which to run cmake
        ##  Assumes:  SPIRAL_HOME is defined (environment variable) or override on command line
        ##  FILEROOT = basename;
        
        print("Compiling and linking C code");
        
        cwd = os.getcwd()
        
        # get module CMakeLists if none exists in current directory
        if not os.path.exists('CMakeLists.txt'):
            module_dir = os.path.dirname(__file__)
            cmfile = os.path.join(module_dir, 'CMakeLists.txt')
            shutil.copy(cmfile, os.getcwd())
            
        tempdir = tempfile.mkdtemp(None, None, cwd)
        os.chdir(tempdir)

        cmake_defroot = '-DFILEROOT:STRING=' + basename
        
        cmd = 'cmake ' + cmake_defroot
        if self._genCuda:
            cmd += ' -DHASCUDA=1'
        elif self._genHIP:
            cmd += ' -DHASHIP=1 -DCMAKE_CXX_COMPILER=hipcc'    
            
        if self._withMPI:
            cmd += ' -DHASMPI=1'
            
        if self._includeMetadata:
            cmd += ' -DHAS_METADATA=1'

        cmd += ' -DPY_LIBS_DIR=' + self._libsDir
        
        if sys.platform == 'win32':
            ##  NOTE: Ensure Python installed on Windows is 64 bit
            cmd += ' .. && cmake --build . --config Release --target install'
            print ( cmd )
            self._runResult = subprocess.run (cmd, shell=True, capture_output=False)
        else:
            cmd += ' .. && make install'
            print ( cmd )
            self._runResult = subprocess.run(cmd, shell=True)

        os.chdir(cwd)

        if self._runResult.returncode == 0 and not self._keeptemp:
            shutil.rmtree(tempdir, ignore_errors=True)
            
    def _setupCFuncs(self, basename):
        script = basename + ".g"
        self._genScript(script)
        self._callSpiral(script)
        if self._includeMetadata:
            self._createMetadataFile(basename)
        self._callCMake(basename)
        
    def _trace(self):
        """Trace execution for generating Spiral script"""
        self._tracingOn = True
        self._callGraph = []
        src = self.buildTestInput()
        self.runDef(src)
        self._tracingOn = False
        for i in range(len(self._callGraph)-1):
            self._callGraph[i] = self._callGraph[i] + ','

    def _initFunc(self):
        """Call the SPIRAL generated init function"""
        funcname = 'init_' + self._namebase
        gf = getattr(self._SharedLibAccess, funcname, None)
        if gf != None:
            ##  print ( 'SWSolver._initFunc: found init_' + self._namebase, flush = True )
            return gf()
        else:
            msg = 'could not find function: ' + funcname
            raise RuntimeError(msg)

    def _func(self, dst, src):
        """Call the SPIRAL generated main function"""
        
        xp = sw.get_array_module(src)
        
        if xp == np: 
            if self._genCuda or self._genHIP:
                raise RuntimeError('GPU function requires CuPy arrays')
            # NumPy array on CPU
            return self._MainFunc( 
                    dst.ctypes.data_as(ctypes.c_void_p),
                    src.ctypes.data_as(ctypes.c_void_p) )
        else:
            if not self._genCuda and not self._genHIP:
                raise RuntimeError('CPU function requires NumPy arrays')
            # CuPy array on GPU
            srcdev = ctypes.cast(src.data.ptr, ctypes.POINTER(ctypes.c_void_p))
            dstdev = ctypes.cast(dst.data.ptr, ctypes.POINTER(ctypes.c_void_p))
            return self._MainFunc(dstdev, srcdev)

        
    def _destroyFunc(self):
        """Call the SPIRAL generated destroy function"""
        funcname = 'destroy_' + self._namebase
        ##  print ( '_destroyFunc: Find destroy func in library, funcname = ' + funcname, flush = True )
        gf = getattr(self._SharedLibAccess, funcname, None)
        if gf != None:
            return gf()
        else:
            msg = 'could not find function: ' + funcname
            raise RuntimeError(msg)

    def embedCube(self, N, src, Ns):
        xp = sw.get_array_module(src)
        retCube = xp.zeros(shape=(N, N, N))
        for k in range(Ns):
            for j in range(Ns):
                for i in range(Ns):
                    retCube[i,j,k] = src[i,j,k]
        if self._tracingOn:
            nnn = '[' + str(N) + ',' + str(N) + ',' + str(N) + ']'
            nsrange = '[0..' + str(Ns-1) + ']'
            nsr3D = '['+nsrange+','+nsrange+','+nsrange+']'
            st = 'ZeroEmbedBox(' + nnn + ', ' + nsr3D + ')'
            self._callGraph.insert(0, st)
        return retCube
		        
    def rfftn(self, x):
        """ forward multi-dimensional real DFT """
        xp = sw.get_array_module(x)
        ret = xp.fft.rfftn(x) # executes z, then y, then x
        if self._tracingOn:
            N = x.shape[0]
            nnn = '[' + str(N) + ',' + str(N) + ',' + str(N) + ']'
            st = 'MDPRDFT(' + nnn + ', -1)'
            self._callGraph.insert(0, st)
        return ret

    def pointwise(self, x, y):
        """ pointwise array multiplication """
        xp = sw.get_array_module(x)
        ret = x * y
        if self._tracingOn:
            nElems = xp.size(x) * 2
            st = 'RCDiag(FDataOfs(symvar, ' + str(nElems) + ', 0))'
            self._callGraph.insert(0, st)
        return ret

    def irfftn(self, x, shape):
        """ inverse multi-dimensional real DFT """
        xp = sw.get_array_module(x)
        ret = xp.fft.irfftn(x, s=shape) # executes x, then y, then z
        if self._tracingOn:
            N = x.shape[0]
            nnn = '[' + str(N) + ',' + str(N) + ',' + str(N) + ']'
            st = 'IMDPRDFT(' + nnn + ', 1)'
            self._callGraph.insert(0, st)
        return ret

    def extract(self, x, N, Nd):
        """ Extract output data of dimension (Nd, Nd, Nd) from the corner of cube (N, N ,N) """
        ret = x[N-Nd:N, N-Nd:N, N-Nd:N]
        if self._tracingOn:
            nnn = '[' + str(N) + ',' + str(N) + ',' + str(N) + ']'
            ndrange = '[' + str(N-Nd) + '..' + str(N-1) + ']'
            ndr3D = '[' + ndrange + ',' + ndrange + ',' + ndrange + ']'
            st = 'ExtractBox(' + nnn + ', ' + ndr3D + ')'
            self._callGraph.insert(0, st)
        return ret

