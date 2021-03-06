import atexit
import os
import shutil
import signal
import tempfile
from concurrent.futures import ThreadPoolExecutor

import spylon.spark

spark_session = None
spark_jvm_helpers = None
scala_intp = None


def init_spark_session(conf=None, application_name="ScalaMetaKernel"):
    # Ensure we have the correct classpath settings for the repl to work.
    os.environ.setdefault('SPARK_SUBMIT_OPTS', '-Dscala.usejavacp=true')
    global spark_session
    # If we have already initialized a spark session. Don't carry on.
    if spark_session:
        return
    if conf is None:
        conf = spylon.spark.launcher.SparkConfiguration()
    spark_context = conf.spark_context(application_name)
    from pyspark.sql import SparkSession
    spark_session = SparkSession(spark_context)
    from spylon.spark.utils import SparkJVMHelpers
    global spark_jvm_helpers
    spark_jvm_helpers = SparkJVMHelpers(spark_session._sc)


def initialize_scala_interpreter():
    """
    Instantiates the scala interpreter via py4j and pyspark.

    Notes
    -----
    Portions of this have been adapted out of Apache Toree and Zeppelin

    Returns
    -------
    SparkInterpreter
    """
    if spark_session is None:
        init_spark_session()

    from spylon.spark.utils import SparkJVMHelpers
    assert isinstance(spark_jvm_helpers, SparkJVMHelpers)
    from pyspark.sql import SparkSession
    assert isinstance(spark_session, SparkSession)

    jvm = spark_session._jvm
    jconf = spark_session._jsc.getConf()
    bytes_out = jvm.org.apache.commons.io.output.ByteArrayOutputStream()

    io = jvm.java.io

    jprintWriter = io.PrintWriter(bytes_out, True)

    execUri = jvm.System.getenv("SPARK_EXECUTOR_URI")
    jconf.setIfMissing("spark.app.name", "Spark shell")
    # SparkContext will detect this configuration and register it with the RpcEnv's
    # file server, setting spark.repl.class.uri to the actual URI for executors to
    # use. This is sort of ugly but since executors are started as part of SparkContext
    # initialization in certain cases, there's an initialization order issue that prevents
    # this from being set after SparkContext is instantiated.

    output_dir = os.path.abspath(tempfile.mkdtemp())

    def cleanup():
        shutil.rmtree(output_dir, True)
    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    jconf.set("spark.repl.class.outputDir", output_dir)
    if (execUri is not None):
      jconf.set("spark.executor.uri", execUri)

    jars = jvm.org.apache.spark.util.Utils.getUserJars(jconf, True).mkString(":")
    interpArguments = spark_jvm_helpers.to_scala_list(
        ["-Yrepl-class-based", "-Yrepl-outdir", output_dir,
         "-classpath", jars
         ]
    )

    settings = jvm.scala.tools.nsc.Settings()
    settings.processArguments(interpArguments, True)

    # Since we have already instantiated our spark context on the python side, set it in the Main repl class as well
    Main = jvm.org.apache.spark.repl.Main
    jspark_session = spark_session._jsparkSession
    # equivalent to Main.sparkSession = jspark_session
    getattr(Main, "sparkSession_$eq")(jspark_session)
    getattr(Main, "sparkContext_$eq")(jspark_session.sparkContext())

    def start_imain():
        intp = jvm.scala.tools.nsc.interpreter.IMain(settings, jprintWriter)
        intp.initializeSynchronous()
        # TODO : Redirect stdout / stderr to a known pair of files that we can watch.
        """
        System.setOut(new PrintStream(new File("output-file.txt")));
        """

        # Copied directly from Spark
        intp.interpret("""
            @transient val spark = if (org.apache.spark.repl.Main.sparkSession != null) {
                org.apache.spark.repl.Main.sparkSession
              } else {
                org.apache.spark.repl.Main.createSparkSession()
              }
            @transient val sc = {
              val _sc = spark.sparkContext
              if (_sc.getConf.getBoolean("spark.ui.reverseProxy", false)) {
                val proxyUrl = _sc.getConf.get("spark.ui.reverseProxyUrl", null)
                if (proxyUrl != null) {
                  println(s"Spark Context Web UI is available at ${proxyUrl}/proxy/${_sc.applicationId}")
                } else {
                  println(s"Spark Context Web UI is available at Spark Master Public URL")
                }
              } else {
                _sc.uiWebUrl.foreach {
                  webUrl => println(s"Spark context Web UI available at ${webUrl}")
                }
              }
              println("Spark context available as 'sc' " +
                s"(master = ${_sc.master}, app id = ${_sc.applicationId}).")
              println("Spark session available as 'spark'.")
              _sc
            }
            """)
        intp.interpret("import org.apache.spark.SparkContext._")
        intp.interpret("import spark.implicits._")
        intp.interpret("import spark.sql")
        intp.interpret("import org.apache.spark.sql.functions._")
        bytes_out.reset()
        return intp

    imain = start_imain()

    return SparkInterpreter(jvm, imain, bytes_out)


def _scala_seq_to_py(jseq):
    n = jseq.size()
    for i in range(n):
        yield jseq.apply(i)


class ScalaException(Exception):

    def __init__(self, scala_message, *args, **kwargs):
        super(ScalaException, self).__init__(scala_message, *args, **kwargs)
        self.scala_message = scala_message


class SparkInterpreter(object):

    executor = ThreadPoolExecutor(4)

    def __init__(self, jvm, jiloop, jbyteout):
        self._jcompleter = None
        self.jvm = jvm
        self.jiloop = jiloop

        interpreterPkg = getattr(getattr(self.jvm.scala.tools.nsc.interpreter, 'package$'), "MODULE$")
        # = spark_jvm_helpers.import_scala_package_object("scala.tools.nsc.interpreter")
        self.iMainOps = interpreterPkg.IMainOps(jiloop)
        self.jbyteout = jbyteout

    def interpret(self, code, synthetic=False):
        """Interpret a block of scala code.

        If you want to get the result as a python object, follow this will a call to `last_result()`

        Parameters
        ----------
        code : str
        synthetic : bool

        Returns
        -------
        reploutput : str
            String output from the scala REPL.
        """
        try:
            res = self.jiloop.interpret(code, synthetic)
            pyres = self.jbyteout.toByteArray().decode("utf-8")
            # The scala interpreter returns a sentinel case class member here which is typically matched via
            # pattern matching.  Due to it having a very long namespace, we just resort to simple string matching here.
            result = res.toString()
            if result == "Success":
                return pyres
            elif result == 'Error':
                raise ScalaException(pyres)
            elif result == 'Incomplete':
                raise ScalaException(pyres)
            return pyres
        finally:
            self.jbyteout.reset()

    def last_result(self):
        """Retrieves the jvm result object from the previous call to interpret.

        If the result is a supported primitive type it is converted to a python object, otherwise it returns a py4j
        view onto that object.

        Returns
        -------
        object
        """
        # TODO : when evaluating multiline expressions this returns the first result
        lr = self.jiloop.lastRequest()
        res = lr.lineRep().call("$result", spark_jvm_helpers.to_scala_list([]))
        return res

    @property
    def jcompleter(self):
        if self._jcompleter is None:
            jClass = self.jvm.scala.tools.nsc.interpreter.PresentationCompilerCompleter
            self._jcompleter = jClass(self.jiloop)
        return self._jcompleter

    def complete(self, code, pos):
        """Performs code completion for a block of scala code.

        Parameters
        ----------
        code : str
            Scala code to perform completion on
        pos : int
            Cursor position

        Returns
        -------
        List[str]
        """
        c = self.jcompleter
        jres = c.complete(code, pos)
        return list(_scala_seq_to_py(jres.candidates()))

    def is_complete(self, code):
        """Determine if a hunk of code is a complete block of scala.

        Parameters
        ----------
        code : str

        Returns
        -------
        str
            One of 'complete', 'incomplete' or 'invalid'
        """
        try:
            res = self.jiloop.parse().apply(code)
            output_class = res.getClass().getName()
            _, status = output_class.rsplit("$", 1)
            if status == 'Success':
                return 'complete'
            elif status == 'Incomplete':
                return 'incomplete'
            else:
                return 'invalid'

        finally:
            self.jbyteout.reset()

    def get_help_on(self, info):
        """For a given symbol attempt to get some minor help on it in terms of function signature.

        Due to the JVM having no runtime docstring information, the level of detail we can retrieve is rather limited.

        Parameters
        ----------
        info : str
            object name to try and get information for

        Returns
        -------
        str

        """
        code = info + '// typeAt {} {}'.format(0, len(info))
        scala_type = self.complete(code, len(code))
        # When using the // typeAt hint we will get back a list made by
        # "" :: type :: Nil
        # according to https://github.com/scala/scala/blob/2.12.x/src/repl/scala/tools/nsc/interpreter/PresentationCompilerCompleter.scala#L52
        assert len(scala_type) == 2
        # TODO: Given that we have a type here we can interpret some java class reflection to see if we can get some
        #       better results for the function in question
        return scala_type[-1]

    def printHelp(self):
        return self.jiloop.helpSummary()


def get_scala_interpreter():
    """Get the scala interpreter instance.

    If the instance has not yet been created, create it.

    Returns
    -------
    scala_intp : SparkInterpreter
    """
    global scala_intp
    if scala_intp is None:
        scala_intp = initialize_scala_interpreter()

    return scala_intp
