
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
    
    def __init__(self, dims, k=SW_FORWARD):
        self._dims = dims
        self._k = k
        
    def dimensions(self):
        return self._dims

    def dimN(self):
        return self._dims[0]
        
    def direction(self):
        return self._k
        

class SWSolver:
    """Base class for SnowWhite solver."""
    
    def __init__(self, problem: SWProblem, namebase = 'func', opts = {}):
        self._problem = problem
        self._opts = opts
        self._colMajor = self._opts.get(SW_OPT_COLMAJOR, False)
        self._genHIP = (self._opts.get(SW_OPT_PLATFORM, SW_CPU) == SW_HIP)
        self._genCuda = (self._opts.get(SW_OPT_PLATFORM, SW_CPU) == SW_CUDA)
        self._keeptemp = self._opts.get(SW_OPT_KEEPTEMP, os.getenv(SW_KEEPTEMP) != None)
        self._withMPI = self._opts.get(SW_OPT_MPI, False)
        self._printRuleTree = self._opts.get(SW_OPT_PRINTRULETREE, False)
        self._tracingOn = False
        self._callGraph = []
        self._SharedLibAccess = None
        self._MainFunc = None
        self._spiralname = 'spiral'
        self._metadata = dict()
        self._includeMetadata = self._opts.get(SW_OPT_METADATA, False)
        self._workdir = os.getenv(SW_WORKDIR)
        
        # find and possibly create the .libs subdirectory
        moduleDir = os.path.dirname(os.path.realpath(__file__))
        self._libsDir = os.path.join(moduleDir, SW_LIBSDIR)
        os.makedirs(self._libsDir, mode=0o777, exist_ok=True)
        
        if self._genCuda:
            self._namebase = namebase + '_cu'
        elif self._genHIP:
            self._namebase = namebase + '_hip'
        else:
            self._namebase = namebase
            
        self._mainFuncName = self._namebase
        self._initFuncName = 'init_' + self._namebase
        self._destroyFuncName = 'destroy_' + self._namebase
        
        # check first for library built for this specific transform
        sharedLibFullPath = os.path.join(self._libsDir, 'lib' + self._namebase + SW_SHLIB_EXT)

        # if no matching specific library, look in metadata of installed libraries
        # and create one if no matching transform is in an existing installed library
        if not os.path.exists(sharedLibFullPath):
            searchmd = self._metadataForSearch()
            (path, names) = findFunctionsWithMetadata(searchmd)
            if (type(path) is str) and (type(names) is dict) and (len(names) > 2):
                sharedLibFullPath = path
                self._mainFuncName    = names.get(SW_KEY_EXEC, self._mainFuncName)
                self._initFuncName    = names.get(SW_KEY_INIT, self._initFuncName)
                self._destroyFuncName = names.get(SW_KEY_DESTROY, self._destroyFuncName)
            else:
                self._setupCFuncs(self._namebase)

        self._SharedLibAccess = ctypes.CDLL(sharedLibFullPath)
        self._MainFunc = getattr(self._SharedLibAccess, self._mainFuncName)
        if self._MainFunc == None:
            msg = 'could not find function: ' + self._mainFuncName
            raise RuntimeError(msg)
        self._initFunc()

    def __del__(self):
        try:
            # destroy function may not exist if cleaning up after error
            self._destroyFunc()
        except:
            pass
    
    def solve(self):
        raise NotImplementedError()

    def runDef(self):
        raise NotImplementedError()
        
    def _writeScript(self, script_file):
        raise NotImplementedError()
    
    def _genScript(self, filename : str):
        self._trace()
        try:
            script_file = open(filename, 'w')
        except:
            print('Error: Could not open ' + filename + ' for writing', file=sys.stderr)
            return
        timestr = datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y")
        print(file = script_file)
        print("# SPIRAL script generated by " + type(self).__name__, file = script_file)
        print('# ' + timestr, file = script_file)
        print(file = script_file)
        self._writeScript(script_file)
        script_file.close()
        
    def _setFunctionMetadata(self, obj):
        pass
        
    def _buildMetadata(self):
        md = self._metadata
        md[SW_KEY_SPIRALBUILDINFO] = spiralBuildInfo()
        funcmeta = dict()
        md[SW_KEY_TRANSFORMS] = [ funcmeta ]
        funcmeta[SW_KEY_DIRECTION]  = SW_STR_INVERSE if self._problem.direction() == SW_INVERSE else SW_STR_FORWARD
        funcmeta[SW_KEY_PRECISION] = SW_STR_SINGLE if self._opts.get(SW_OPT_REALCTYPE) == "float" else SW_STR_DOUBLE
        funcmeta[SW_KEY_TRANSFORMTYPE] = SW_TRANSFORM_UNKNOWN
        funcmeta[SW_KEY_DIMENSIONS] = self._problem.dimensions()
        funcmeta[SW_KEY_PLATFORM] = self._opts.get(SW_OPT_PLATFORM, SW_CPU)
        names = dict()
        funcmeta[SW_KEY_NAMES] = names
        names[SW_KEY_EXEC] = self._mainFuncName
        names[SW_KEY_INIT] = self._initFuncName
        names[SW_KEY_DESTROY] = 'destroy_' + self._namebase
        self._setFunctionMetadata(funcmeta)
        md[SW_KEY_TRANSFORMTYPES] = [ funcmeta.get(SW_KEY_TRANSFORMTYPE) ]
    
    def _createMetadataFile(self, basename):
        """Write metadata source file."""
        varname  = basename + SW_METAVAR_EXT
        filename = basename + SW_METAFILE_EXT
        self._buildMetadata()
        writeMetadataSourceFile(self._metadata, varname, filename) 

    def _metadataForSearch(self):
        funcmeta = dict()
        funcmeta[SW_KEY_DIRECTION]  = SW_STR_INVERSE if self._problem.direction() == SW_INVERSE else SW_STR_FORWARD
        funcmeta[SW_KEY_PRECISION] = SW_STR_SINGLE if self._opts.get(SW_OPT_REALCTYPE) == "float" else SW_STR_DOUBLE
        funcmeta[SW_KEY_TRANSFORMTYPE] = SW_TRANSFORM_UNKNOWN
        funcmeta[SW_KEY_DIMENSIONS] = self._problem.dimensions()
        funcmeta[SW_KEY_PLATFORM] = self._opts.get(SW_OPT_PLATFORM, SW_CPU)
        self._setFunctionMetadata(funcmeta)
        return funcmeta

    def _callSpiral(self, script):
        """Run SPIRAL with script as input."""
        if self._genCuda:
            print ( 'Generating CUDA', flush = True )
        elif self._genHIP:
            print ( 'Generating HIP', flush = True )
        else:
            print ( 'Generating C', flush = True )
        return callSpiralWithFile(script)

    def _callCMake (self, basename):
        ##  Assumes:  SPIRAL_HOME is defined (environment variable) or override on command line
        ##  FILEROOT = basename;
        
        print("Compiling and linking");
        
        # copy module CMakeLists to current directory
        module_dir = os.path.dirname(__file__)
        cmfile = os.path.join(module_dir, 'CMakeLists.txt')
        shutil.copy(cmfile, os.getcwd())

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
            cmd += ' . && cmake --build . --config Release --target install'
        else:
            cmd += ' . && make install'
            
        runResult = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if runResult.returncode != 0:
            print(runResult.stderr.decode(), file=sys.stderr)
        
        return runResult.returncode
            
    def _setupCFuncs(self, basename):
        # if workdir specified, cd to it
        if self._workdir != None:
            try:
                os.chdir(self._workdir)
            except:
                print('Could not find workdir "' + str(self._workdir) + '". Using current directory.')
    
        # create temporary build directory and cd to it
        cwd = os.getcwd()
        tempdir = tempfile.mkdtemp(None, basename + '_', cwd)
        os.chdir(tempdir)
    
        script = basename + ".g"
        self._genScript(script)
        ret = self._callSpiral(script)
        if ret == SPIRAL_RET_OK:
            if self._includeMetadata:
                self._createMetadataFile(basename)
        else:
            # return to original working directory and raise error
            os.chdir(cwd)
            msg = 'SPIRAL error'
            raise RuntimeError(msg)
        
        ret = self._callCMake(basename)
        
        # return to original working directory
        os.chdir(cwd)
        
        if ret != 0:
            msg = "CMake error"
            raise RuntimeError(msg)
        
        # optionally remove temp dir
        if (not self._keeptemp):
            shutil.rmtree(tempdir, ignore_errors=True)
        
        return
        
    def buildTestInput(self):
        raise NotImplementedError()
            
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
        gf = getattr(self._SharedLibAccess, self._initFuncName, None)
        if gf != None:
            ##  print ( 'SWSolver._initFunc: found init_' + self._namebase, flush = True )
            return gf()
        else:
            msg = 'could not find function: ' + self._initFuncName
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
        gf = getattr(self._SharedLibAccess, self._destroyFuncName, None)
        if gf != None:
            return gf()
        else:
            msg = 'could not find function: ' + self._destroyFuncName
            raise RuntimeError(msg)

    def zeroEmbedBox(self, src, padding):
        xp = sw.get_array_module(src)
        retCube = xp.pad(src, padding)
        if self._tracingOn:
            t1 = padding[0]
            t2 = padding[1] if len(padding) > 1 else t1
            t3 = padding[2] if len(padding) > 2 else t2
            n1 = src.shape[0]
            n2 = src.shape[1]
            n3 = src.shape[2]
            N1 = t1[0] + n1 + t1[1]
            N2 = t2[0] + n2 + t2[1]
            N3 = t3[0] + n3 + t3[1]
            nnn = '[' + str(N1) + ',' + str(N2) + ',' + str(N3) + ']'
            nsrange1 = '[{}..{}]'.format(t1[0], t1[0] + n1 - 1)
            nsrange2 = '[{}..{}]'.format(t2[0], t2[0] + n2 - 1)
            nsrange3 = '[{}..{}]'.format(t3[0], t3[0] + n3 - 1)
            nsr3D = '['+nsrange1+','+nsrange2+','+nsrange3+']'
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

