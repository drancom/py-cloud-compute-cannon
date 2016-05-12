"""
Functions to that allow python commands to be run as jobs with the platform API
"""
import cPickle as cp
import inspect

import sys

import pyccc
import pyccc.utils as utils
from pyccc import job
from pyccc import source_inspections as src
from pyccc.files import StringContainer, LocalFile

__all__ = 'PythonCall PythonLauncher PythonJob'.split()


PYTHON_JOB_FILE = LocalFile('%s/static/run_job.py' % pyccc.bioplatform_path)


class PythonCall(object):
    def __init__(self, function, args=None, kwargs=None):
        self.function = function
        self.args = args if args is not None else []
        self.kwargs = kwargs if kwargs is not None else {}

        try:
            temp = function.im_self.__class__
        except AttributeError:
            self.is_instancemethod = False
        else:
            self.is_instancemethod = True

    def __call__(self):
        return self.function(self.args, self.kwargs)


class PythonLauncher(job.Launcher):
    # @utils.doc_inherit
    def __init__(self, *args, **kwargs):
        super(PythonLauncher, self).__init__(*args, **kwargs)
        self.create_job = PythonJob

    def __call__(self, func, *args, **kwargs):
        command = PythonCall(func, args, kwargs)
        return super(PythonLauncher, self).__call__(command)

    def prepare(self, func, *args, **kwargs):
        """TODO: merge prepare and __call__ with a descriptor"""
        command = PythonCall(func, args, kwargs)
        return super(PythonLauncher, self).prepare(command)


class PythonJob(job.Job):
    SHELL_COMMAND = 'python2 run_job.py'

    # @utils.doc_inherit
    def __init__(self, engine, image, command, sendsource=True, **kwargs):
        self._raised = False
        self._updated_object = None
        self._exception = None
        self._traceback = None
        self.sendsource = sendsource

        self.function_call = command

        if 'inputs' not in kwargs:
            kwargs['inputs'] = {}
        inputs = kwargs['inputs']

        # assemble the commands to run
        python_files = self._get_python_files()
        inputs.update(python_files)

        super(PythonJob, self).__init__(engine, image, self.SHELL_COMMAND,
                                        **kwargs)

    def _get_python_files(self):
        """
        Construct the files to send the remote host
        :return: dictionary of filenames and file objects
        """
        python_files = {'run_job.py': PYTHON_JOB_FILE}

        remote_function = PackagedFunction(self.function_call.function,
                                           *self.function_call.args,
                                           **self.function_call.kwargs)
        python_files['function.pkl'] = StringContainer(cp.dumps(remote_function, protocol=2),
                                                       'function.pkl')

        sourcefile = StringContainer(self._get_source(), 'source.py')

        python_files['source.py'] = sourcefile
        return python_files

    def _get_source(self):
        """
        Calls the approriate source inspection to get any requires source code
        :return: string containing source code
        """
        if self.sendsource:
            func = self.function_call.function
            if self.function_call.is_instancemethod:
                obj = func.im_self.__class__
            else:
                obj = func
            return src.getsource(obj)
        elif self.function_call.is_instancemethod:
            return ''
        else:
            return "from %s import %s\n" % (self.function_call.function.__module__,
                                            self.function_call.function.__name__)

    @property
    def result(self):
        """
        Python function's return value.
        Will re-raise any exceptions raised remotely
        """
        if self._callback_result is None:
            self.reraise_remote_exception(force=True)  # there's no result to return
            self._callback_result = cp.loads(self.get_output('_function_return.pkl').read())
        return self._callback_result

    def finish(self):
        self.wait()
        sys.stderr.write(self.stderr)
        sys.stdout.write(self.stdout)
        return self.result

    @property
    def updated_object(self):
        """
        If the function was an object's method, return the new state of the object
        Will re-raise any exceptions raised remotely
        """
        if self._updated_object is None:
            self.reraise_remote_exception()
            self._updated_object = cp.loads(self.get_output('_object_state.pkl').read())
        return self._updated_object

    @property
    def exception(self):
        """
        The exception object, if any, from the remote execution
        """
        if self._exception is None:
            if 'exception.pkl' in self.get_output():
                self._raised = False
                try:
                    self._exception = cp.loads(self.get_output('exception.pkl').read())
                except Exception as exc:  # catches errors in unpickling the exception
                    self._exception = exc
                self._traceback = self.get_output('traceback.txt').read()
            else:
                self._exception = False
        return self._exception

    def reraise_remote_exception(self, force=False):
        """
        Raises exceptions from the remote execution
        """
        # TODO: include debugging info / stack variables using tblib? - even if possible, this won't work without deps
        # TODO: make it clear that this is a *remote* exception, include a traceback for the last raise?
        import tblib
        if (force or not self._raised) and self.exception:
            self._raised = True
            raise self._exception, None, tblib.Traceback.from_string(self._traceback).as_traceback()


class PackagedFunction(object):
    """
    This object captures enough information to serialize, deserialize, and run a
    python function
    """
    def __init__(self, func, *args, **kwargs):
        """
        Conduct and store enough introspection on the passed function and arguments so
        that they can be serialized and run elsewhere
        """
        # TODO: check whether pickled object can be successfully unpickled
        # i.e., make sure its functions and classes are defined in the
        # base code

        # Store the function, arguments (and its object if necessary)
        self.is_imethod = hasattr(func, 'im_self')
        if self.is_imethod:
            self.obj = func.im_self
            self.imethod_name = func.__name__
        else:
            self.func_name = func.__name__
        self.args = args
        self.kwargs = kwargs

        # Store any methods or variables bound from the function's closure
        closure = src.getclosurevars(func)
        if closure.nonlocals:
            raise TypeError("Can't launch a job with closure variables: %s"%
                            closure.nonlocals.keys() )
        self.global_closure = {}
        self.global_modules = {}
        for name, value in closure.globals.iteritems():
            if inspect.ismodule(value):
                self.global_modules[name] = value.__name__
            else:
                self.global_closure[name] = value

    def run(self, func=None):
        """
        Evaluates the packaged function as func(*self.args,**self.kwargs)
        If func is a method of an object, it's accessed as getattr(self.obj,func_name).
        If it's a user-defined function, it needs to be passed in here because it can't
        be serialized.
        :return: function's return value
        """
        to_run = self.prepare_namespace(func)
        result = to_run(*self.args, **self.kwargs)
        return result

    def prepare_namespace(self, func):
        """
        Prepares the function to be run after deserializing it.
        Re-associates any previously bound variables and modules from the closure
        :return: Prepared callable function
        """
        if self.is_imethod:
            to_run = getattr(self.obj, self.imethod_name)
        else:
            to_run = func

        for varname, modulename in self.global_modules.iteritems():
            exec ('import %s as %s' % (modulename, varname))
            to_run.func_globals[varname] = eval(varname)
        for name, value in self.global_closure.iteritems():
            to_run.func_globals[name] = value
        return to_run