"""
FeatureResponses and associated functions and classes.

These classes implement map and tuning curve measurement based
on measuring responses while varying features of an input pattern.
"""

import copy

from math import pi
from colorsys import hsv_to_rgb

import numpy as np

import param
from param.parameterized import ParameterizedFunction, ParamOverrides, bothmethod

import topo
import topo.base.sheetcoords
from topo.base.arrayutil import wrap
from topo.base.cf import CFSheet
from topo.base.functionfamily import PatternDrivenAnalysis
from topo.base.sheet import Sheet, activity_type
from topo.base.sheetview import SheetView
from topo.command import restore_input_generators, save_input_generators
from topo.misc.distribution import Distribution, DistributionStatisticFn, DSF_MaxValue, DSF_WeightedAverage
from topo.misc.util import cross_product, frange
from topo import pattern
from topo.pattern import SineGrating, Gaussian, RawRectangle, Disk
from topo.plotting.plotgroup import plotgroups
from topo.sheet import GeneratorSheet


activity_dtype = np.float64

# CB: having a class called DistributionMatrix with an attribute
# distribution_matrix to hold the distribution matrix seems silly.
# Either rename distribution_matrix or make DistributionMatrix into
# a matrix.
class DistributionMatrix(param.Parameterized):
    """
    Maintains a matrix of Distributions (each of which is a dictionary
    of (feature value: activity) pairs).

    The matrix contains one Distribution for each unit in a
    rectangular matrix (given by the matrix_shape constructor
    argument).  The contents of each Distribution can be updated for a
    given bin value all at once by providing a matrix of new values to
    update().

    The results can then be accessed as a matrix of weighted averages
    (which can be used as a preference map) and/or a selectivity map
    (which measures the peakedness of each distribution).
    """

    def __init__(self,matrix_shape,axis_range=(0.0,1.0),cyclic=False,keep_peak=True):
        """Initialize the internal data structure: a matrix of Distribution objects."""
        self.axis_range=axis_range
        new_distribution = np.vectorize(lambda x: Distribution(axis_range,cyclic,keep_peak),
                                     doc="Return a Distribution instance for each element of x.")
        self.distribution_matrix = new_distribution(np.empty(matrix_shape))


    def update(self,new_values,bin):
        """Add a new matrix of histogram values for a given bin value."""
        ### JABHACKALERT!  The Distribution class should override +=,
        ### rather than + as used here, because this operation
        ### actually modifies the distribution_matrix, but that has
        ### not yet been done.  Alternatively, it could use a different
        ### function name altogether (e.g. update(x,y)).
        self.distribution_matrix + np.fromfunction(np.vectorize(lambda i,j: {bin:new_values[i,j]}),
                                                new_values.shape)

    def apply_DSF(self,dsf):
        """
        Apply the given dsf DistributionStatisticFn on each element of
        the distribution_matrix

        Return a dictionary of dictionaries, with the same structure
        of the called DistributionStatisticFn, but with matrices as
        values, instead of scalars
        """

        shape = self.distribution_matrix.shape
        result = {}

        # this is an extra call to the dsf() DistributionStatisticFn, in order to retrieve
        # the dictionaries structure, and allocate the necessary matrices
        r0 = dsf(self.distribution_matrix[0,0])
        for k,maps in r0.items():
            result[k] = {}
            for m in maps.keys():
                result[k][m] = np.zeros(shape,np.float64)

        for i in range(shape[0]):
            for j in range(shape[1]):
                response = dsf(self.distribution_matrix[i,j])
                for k,d in response.items():
                    for item,item_value in d.items():
                        result[k][item][i,j] = item_value

        return result



class FullMatrix(param.Parameterized):
    """
    Records the output of every unit in a sheet, for every combination
    of feature values.  Useful for collecting data for later analysis
    while presenting many input patterns.
    """

    def __init__(self,matrix_shape,features):
        self.matrix_shape = matrix_shape
        self.features = features
        self.dimensions = ()
        for f in features:
            self.dimensions = self.dimensions+(np.size(f.values),)
        self.full_matrix = np.empty(self.dimensions,np.object_)


    def update(self,new_values,feature_value_permutation):
        """Add a new matrix of histogram values for a given bin value."""
        index = ()
        for f in self.features:
            for ff,value in feature_value_permutation:
                if(ff == f.name):
                    index = index+(f.values.index(value),)
        self.full_matrix[index] = new_values


class FeatureResponses(PatternDrivenAnalysis):
    """
    Systematically vary input pattern feature values and collate the
    responses.

    A DistributionMatrix for each measurement source and feature is
    created.  The DistributionMatrix stores the distribution of
    activity values for that feature.  For instance, if the features
    to be tested are orientation and phase, we will create a
    DistributionMatrix for orientation and a DistributionMatrix for
    phase for each measurement source.  The orientation and phase of
    the input are then systematically varied (when measure_responses
    is called), and the responses of all units from a measurement
    source to each pattern are collected into the DistributionMatrix.

    The resulting data can then be used to plot feature maps and
    tuning curves, or for similar types of feature-based analyses.
    """

    cmd_overrides = param.Dict(default={},doc="""
        Dictionary used to overwrite default values of the
        pattern_response_fn.""")

    io_dimensions_hook = param.Callable(default=None,instantiate=True,doc="""
        Interface function, which should return the name and dimensions
        of the input and output sources for pattern presentation and
        response measurement.""")

    measurement_prefix = param.String(default="",doc="""
        Prefix to add to the name under which results are stored.""")

    measurement_storage_hook = param.Callable(default=None,instantiate=True,doc="""
        Interface to store measurements after they have been completed.""")

    param_dict = param.Dict(default={},doc="""
        Dictionary containing name value pairs of a feature, which is to
        be varied across measurements.""")

    pattern_coordinator = param.Callable(default=None,instantiate=True,doc="""
        Coordinates the creation and linking of numerous simultaneously
        presented input patterns, controlled by complex features.""")

    pattern_response_fn = param.Callable(default=None,instantiate=True,doc="""
        Presenter command responsible for presenting the input
        patterns provided to it, returning measurement labels and
        collecting and storing the measurement results in the
        appropriate place.""")

    repetitions = param.Integer(default=1,bounds=(1,None),doc="""
        How many times each stimulus will be presented.

        Each stimulus is specified by a particular feature
        combination, and need only be presented once if the network
        has no other source of variability.  If results differ for
        each presentation of an identical stimulus (e.g. due to
        intrinsic noise), then this parameter can be increased
        so that results will be an average over the specified
        number of repetitions.""")

    store_fullmatrix = param.Boolean(default=False,doc="""
        Determines whether or not store the full matrix of feature
        responses as a class attribute.""")

    _fullmatrix = {}

    __abstract = True


    def _initialize_featureresponses(self,p):
        """
        Create an empty DistributionMatrix for each feature and each
        measurement source, in addition to activity buffers and if
        requested, the full matrix.
        """
        self.input_shapes, self.response_shapes = p.io_dimensions_hook()
        self._apply_cmd_overrides(p)

        self._featureresponses = {}
        self._activities = {}

        if p.store_fullmatrix:
            FeatureResponses._fullmatrix = {}

        for response_label,shape in self.response_shapes.items():
            self._featureresponses[response_label] = {}
            self._activities[response_label]=np.zeros(shape)
            for f in self.features:
                self._featureresponses[response_label][f.name]=DistributionMatrix(shape,
                                                                                  axis_range=f.range,
                                                                                  cyclic=f.cyclic)
            if p.store_fullmatrix:
                FeatureResponses._fullmatrix[response_label] = FullMatrix(shape,self.features)


    def _measure_responses(self,p):
        """
        Generate feature permutations and present each in sequence.
        """

        # Run hooks before the analysis session
        for f in p.pre_analysis_session_hooks: f()

        features_to_permute = [f for f in self.features if f.compute_fn is None]
        self.features_to_compute = [f for f in self.features if f.compute_fn is not None]

        self.feature_names=[f.name for f in features_to_permute]
        values_lists=[f.values for f in features_to_permute]

        self.permutations=cross_product(values_lists)
        values_description=' * '.join(["%d %s" % (len(f.values),f.name) for f in features_to_permute])

        self.total_steps = len(self.permutations) * p.repetitions - 1
        for permutation_num,permutation in enumerate(self.permutations):
            try:
                self._present_permutation(p,permutation,permutation_num)
            except MeasurementInterrupt as MI:
                self.warning("Measurement was stopped after {current} out of {total} presentations. " \
                             "Results may be incomplete.".format(current=MI.current,total=MI.total))
                break

        # Run hooks after the analysis session
        for f in p.post_analysis_session_hooks: f()


    def _present_permutation(self,p,permutation,permutation_num):
        """Present a pattern with the specified set of feature values."""
        for response_label in self.response_shapes:
            self._activities[response_label]*=0

        # Calculate complete set of settings
        permuted_settings = zip(self.feature_names,permutation)
        complete_settings = permuted_settings + \
            [(f.name,f.compute_fn(permuted_settings)) for f in self.features_to_compute]

        for i in xrange(0,p.repetitions):
            for f in p.pre_presentation_hooks: f()

            inputs = p.pattern_coordinator(dict(permuted_settings),p.param_dict,self.input_shapes.keys())
            p.pattern_response_fn(inputs,self._activities,p.repetitions*permutation_num+i,self.total_steps)

            for f in p.post_presentation_hooks: f()

        for response_label in self.response_shapes:
            self._activities[response_label] = self._activities[response_label] / p.repetitions

        self._update(p,complete_settings)


    def _update(self,p,current_values):
        """
        Update each DistributionMatrix with (activity,bin) and
        populate the full matrix, if enabled.
        """
        for response_label in self.response_shapes:
            for feature,value in current_values:
                self._featureresponses[response_label][feature].update(self._activities[response_label],value)
            if p.store_fullmatrix:
                FeatureResponses._fullmatrix[response_label].update(self._activities[response_label],current_values)


    def _apply_cmd_overrides(self,p):
        """
        Applies the cmd_overrides to the pattern_response_fn and
        the pattern_coordinator before launching a measurement.
        """

        for override,value in p.cmd_overrides.items():
            if override in p.pattern_response_fn.params():
                p.pattern_response_fn.set_param(override,value)
            if override in p.pattern_coordinator.params():
                p.pattern_coordinator.set_param(override,value)


    @bothmethod
    def set_cmd_overrides(self_or_cls,**overrides):
        """
        Allows setting of cmd_overrides at the class and instance
        level.
        """
        self_or_cls.cmd_overrides = dict(self_or_cls.cmd_overrides,**overrides)



class FeatureMaps(FeatureResponses):
    """
    Measure and collect the responses to a set of features, for
    calculating feature maps.

    For each feature and each measurement source, the results are
    stored as a preference matrix and selectivity matrix in the
    sheet's sheet_views; these can then be plotted as preference or
    selectivity maps.
    """

    preference_fn = param.ClassSelector(DistributionStatisticFn,
                                        default=DSF_WeightedAverage(),doc="""
        Function for computing a scalar-valued preference,
        selectivity, etc. from the distribution of responses.  Note
        that this default is overridden by specific functions for
        individual features, if specified in the Feature objects.""")

    selectivity_multiplier = param.Number(default=17.0,doc="""
        Scaling of the feature selectivity values, applied in all
        feature dimensions.  The multiplier sets the output
        scaling.  The precise value is arbitrary, and set to match
        historical usage.""")

    # CBENHANCEMENT: could allow full control over the generated names
    # using a format parameter. The default would be
    # ${prefix}${feature}${type} (where type is Preference or
    # Selectivity)

    def __call__(self,features,**params):
        """
        Present the given input patterns and collate the responses.

        Responses are statistics on the distributions of measure for
        every unit, extracted by functions that are subclasses of
        DistributionStatisticFn, and could be specified in each
        feature with the preference_fn parameter, otherwise the
        default in self.preference_fn is used.
        """
        p = ParamOverrides(self,params,allow_extra_keywords=True)
        self.features=features

        self._initialize_featureresponses(p)
        self._measure_responses(p)
        result_dict = {}
        result_dict['FeatureMaps'] = {}

        for response_label in self.response_shapes:
            result_dict['FeatureMaps'][response_label] = {}
            for feature in self._featureresponses[response_label]:
                fp = filter(lambda f: f.name==feature,self.features)[0]
                ar = self._featureresponses[response_label][feature].distribution_matrix[0,0].axis_range
                cyclic_range = ar if fp.cyclic else 1.0
                preference_fn = fp.preference_fn if fp.preference_fn is not None else self.preference_fn
                if p.selectivity_multiplier is not None:
                    preference_fn.selectivity_scale = (preference_fn.selectivity_scale[0],self.selectivity_multiplier)
                fr = self._featureresponses[response_label][feature]
                response = fr.apply_DSF(preference_fn)
                base_name = self.measurement_prefix + feature.capitalize()

                for k,maps in response.items():
                    for map_name,map_view in maps.items():
                        name = base_name + k + map_name.capitalize()
                        result_dict['FeatureMaps'][response_label][name] = (map_view,
                                                          False if map_name == 'selectivity' else fp.cyclic,
                                                          None  if map_name == 'selectivity' else cyclic_range)

        if p.measurement_storage_hook:
            p.measurement_storage_hook(result_dict)

        return self._fullmatrix


class FeatureCurves(FeatureResponses):
    """
    Measures and collects the responses to a set of features, for
    calculating tuning and similar curves.

    These curves represent the response of a measurement source to
    patterns that are controlled by a set of features.  This class can
    collect data for multiple curves, each with the same x axis.  The
    x axis represents the main feature value that is being varied,
    such as orientation.  Other feature values can also be varied,
    such as contrast, which will result in multiple curves (one per
    unique combination of other feature values).

    The measured responses used to construct the curves will be passed
    to the presenter_cmd to be stored.  A particular set of patterns
    is then constructed using a user-specified PatternPresenter by
    adding the parameters determining the curve (curve_param_dict) to
    a static list of parameters (param_dict), and then varying the
    specified set of features.  The results can be accessed in the
    curve_dict passed to the presenter_cmd, indexed by the curve_label
    and feature value.
    """

    x_axis = param.String(default=None,doc="""
        Parameter to use for the x axis of tuning curves.""")

    post_collect_responses_hook = param.HookList(default=[],instantiate=False,doc="""
        List of callable objects to be run at the end of collect_feature_responses function.
        The functions should accept three parameters: FullMatrix, curve label, sheet""")

    curve_label = param.String(default=None,doc="""
        Curve label, specifying the value along some feature dimension.""")


    def __call__(self,features,**params):
        p = ParamOverrides(self,params,allow_extra_keywords=True)
        self.features=features
        self._initialize_featureresponses(p)
        self._measure_responses(p)
        result_dict = {}
        result_dict['FeatureCurves'] = {}
        for response_label,shape in self.response_shapes.items():
            result_dict['FeatureCurves'][response_label] = {}
            rows,cols = shape
            for key in self._featureresponses[response_label][p.x_axis].distribution_matrix[0,0]._data.iterkeys():
                y_axis_values = np.zeros(shape,activity_type)
                for i in range(rows):
                    for j in range(cols):
                        y_axis_values[i,j] = self._featureresponses[response_label][p.x_axis].distribution_matrix[i,j].get_value(key)
                result_dict['FeatureCurves'][response_label][key] = (p.measurement_prefix,p.curve_label,p.x_axis,y_axis_values)

        if p.measurement_storage_hook:
            p.measurement_storage_hook(result_dict)

        if p.store_fullmatrix:
            for f in self.post_collect_responses_hook: f(self._fullmatrix[self.sheet],curve_label,self.sheet)
        elif len(self.post_collect_responses_hook) > 0:
            self.warning("Post_collect_responses_hooks require fullmatrix to be enabled.""")

        return result_dict


class ReverseCorrelation(FeatureResponses):
    """
    Calculate the receptive fields for all neurons using reverse correlation.
    """

    continue_measurement = param.Boolean(default=True)

    def _initialize_featureresponses(self,p):
        self.input_shapes, self.response_shapes = p.io_dimensions_hook()
        self._apply_cmd_overrides(p)

        self._activities = {}
        self._featureresponses = {}

        for label,shape in self.response_shapes.items()+self.input_shapes.items():
            self._activities[label]=np.zeros(shape,dtype=activity_dtype)

        for input_label,input_shape in self.input_shapes.items():
            self._featureresponses[input_label] = {}
            for response_label,response_shape in self.response_shapes.items():
                rows,cols = response_shape
                self._featureresponses[input_label][response_label] = np.array([[np.zeros(input_shape,dtype=activity_dtype)
                                                                                 for r in range(rows)]
                                                                                for c in range(cols)])


    def __call__(self,features,**params):
        p = ParamOverrides(self,params,allow_extra_keywords=True)
        self.features=features

        self._initialize_featureresponses(p)
        self._measure_responses(p)

        rf_dict = {}
        rf_dict['ReverseCorrelation'] = {}

        for input_label,input_shape in self.input_shapes.items():
            rf_dict['ReverseCorrelation'][input_label] = {}
            for response_label,response_shape in self.response_shapes.items():
                rf_dict['ReverseCorrelation'][input_label][response_label] = {}
                rows,cols = response_shape
                for ii in range(rows):
                    for jj in range(cols):
                        rf_dict['ReverseCorrelation'][input_label][response_label][(ii,jj)] = self._featureresponses[input_label][response_label][ii,jj]

        if p.measurement_storage_hook:
            p.measurement_storage_hook(rf_dict)

        return rf_dict


    def _present_permutation(self,p,permutation,permutation_num,total_steps):
        """Present a pattern with the specified set of feature values."""

        for label in self.response_shapes.keys()+self.input_shapes.keys():
            self._activities[label]*=0

        # Calculate complete set of settings
        permuted_settings = zip(self.feature_names,permutation)
        complete_settings = permuted_settings + \
            [(f.name,f.compute_fn(permuted_settings)) for f in self.features_to_compute]

        # Run hooks before and after pattern presentation.
        for f in p.pre_presentation_hooks: f()

        inputs = p.pattern_coordinator(dict(permuted_settings),p.param_dict,self.input_shapes.keys())

        response_dict = p.pattern_response_fn(inputs,permutation_num,total_steps)
        p.pattern_response_fn(inputs,self._activities,permutation_num,self.total_steps)

        for f in p.post_presentation_hooks: f()

        self._update(p)


    # Ignores current_values; they simply provide distinct patterns on the retina
    def _update(self,p):
        for input_label in self.input_shapes:
            for response_label,response_shape in self.response_shapes.items():
                rows,cols = response_shape
                for ii in range(rows):
                    for jj in range(cols):
                        delta_rf = self._activities[response_label][ii,jj]*self._activities[input_label]
                        self._featureresponses[input_label][response_label][ii,jj]+=delta_rf


###############################################################################
###############################################################################

# Define user-level commands and helper classes for calling the above


class Feature(param.Parameterized):
    """
    Specifies several parameters required for generating a map of one input feature.
    """

    name = param.String(default="",doc="Name of the feature to test")

    cyclic = param.Boolean(default=False,doc="""
            Whether the range of this feature is cyclic (wraps around at the high end)""")

    compute_fn = param.Callable(default=None,doc="""
            If non-None, a function that when given a list of other parameter values,
            computes and returns the value for this feature""")

    preference_fn = param.ClassSelector(DistributionStatisticFn,default=DSF_WeightedAverage(),
            doc="""Function that will be used to analyze the distributions of unit response
            to this feature""")

    range = param.NumericTuple(default=(0,0), doc="""
            lower and upper values for a feature,used to build a list of values,
            together with the step parameter""")

    step = param.Number(default=0.0,doc="""
            increment used to build a list of values for this feature, together with
            the range parameter""")

    offset = param.Number(default=0.0,doc="offset to add to the values for this feature")

    values = param.List(default=[],doc="""
            explicit list of values for this feature, used in alternative to the range
            and step parameters""")


    def __init__(self,**params):
        """
        Users can provide either a range and a step size, or a list of
        values.  If a list of values is supplied, the range can be
        omitted unless the default of the min and max in the list of
        values is not appropriate.

        If non-None, the compute_fn should be a function that when
        given a list of other parameter values, computes and returns
        the value for this feature.

        If supplied, the offset is added to the given or computed
        values to allow the starting value to be specified.

        """

        super(Feature,self).__init__(**params)

        if len(self.values):
            self.values = self.values if self.offset == 0 else [v+self.offset for v in self.values]
            if self.range == (0,0):
                self.range = (min(self.values),max(self.values))
        else:
            if self.range == (0,0):
                raise ValueError('The range or values must be specified.')
            low_bound,up_bound = self.range
            self.values = frange(low_bound,up_bound,self.step,not self.cyclic)
            self.values = self.values if self.offset == 0 else \
                    [(v + self.offset) % (up_bound - low_bound) if self.cyclic else (v + self.offset)
                    for v in self.values]


# PJFRALERT: This class should eventually be replaced by higher level
# features, which allow you to coordinate input patterns. As such it
# currently still references input sheets and other Topographica
# specific objects.
class CoordinatedPatternGenerator(param.Parameterized):
    """
    This class helps coordinate a set of patterns to be presented to a
    set of GeneratorSheets.  It provides a standardized way of
    generating a set of linked patterns for testing or analysis, such
    as when measuring preference maps or presenting test patterns.
    Subclasses can provide additional mechanisms for doing this in
    different ways.

    This class should eventually be replaced with coordinated or
    complex features, which handle special cases like different
    contrasts, direction or hue values, requiring concerted changes to
    the input pattern.
    """

    pattern_generator = param.Callable(instantiate=True,default=None)

    # JABALERT: Needs documenting, and probably also a clearer name
    contrast_parameter = param.Parameter('michelson_contrast')

    # JABALERT: Needs documenting; apparently only for retinotopy?
    divisions = param.Parameter()

    duration = param.Number(default=None,doc="""
         Duration of pattern presentation, required for some motion stimuli.""")

    def __init__(self,**params):
        super(CoordinatedPatternGenerator,self).__init__(**params)


    def __call__(self,features_values,param_dict,input_names):
        for param,value in param_dict.iteritems():
            setattr(self.pattern_generator,param,value)

        for feature,value in features_values.iteritems():
            setattr(self.pattern_generator,feature,value)

        all_input_names = topo.sim.objects(GeneratorSheet).keys()

        if len(input_names)==0:
             input_names = all_input_names

        # Copy the given generator once for every input
        inputs = dict.fromkeys(input_names)
        for k in inputs.keys():
            inputs[k]=copy.deepcopy(self.pattern_generator)

        ### JABALERT: Should replace these special cases with general
        ### support for having meta-parameters controlling the
        ### generation of different patterns for each GeneratorSheet.
        ### For instance, we will also need to support xdisparity and
        ### ydisparity, plus movement of patterns between two eyes, colors,
        ### etc.  At the very least, it should be simple to control
        ### differences in single parameters easily.  In addition,
        ### these meta-parameters should show up as parameters for
        ### this object, augmenting the parameters for each individual
        ### pattern, e.g. in the Test Pattern window.  In this way we
        ### should be able to provide general support for manipulating
        ### both pattern parameters and parameters controlling
        ### interaction between or differences between patterns.

        if 'direction' in features_values:
            import __main__
            if '_new_motion_model' in __main__.__dict__ and __main__.__dict__['_new_motion_model']:
            #### new motion model ####
                from topo.pattern import Translator
                for name in inputs:
                    inputs[name] = Translator(generator=inputs[name],
                                              direction=features_values['direction'],
                                              speed=features_values['speed'],
                                              reset_period=self.duration)
            ##########################
            else:
            #### old motion model ####
                orientation = features_values['direction']+pi/2
                from topo.pattern import Sweeper
                for name in inputs.keys():
                    speed=features_values['speed']
                    try:
                        step=int(name[-1])
                    except:
                        if not hasattr(self,'direction_warned'):
                            self.warning('Assuming step is zero; no input lag number specified at the end of the input sheet name.')
                            self.direction_warned=True
                        step=0
                    speed=features_values['speed']
                    inputs[name] = Sweeper(generator=inputs[name],step=step,speed=speed)
                    setattr(inputs[name],'orientation',orientation)
            ##########################

        if features_values.has_key('hue'):

            # could be three retinas (R, G, and B) or a single RGB
            # retina for the color dimension; if every retina has
            # 'Red' or 'Green' or 'Blue' in its name, then three
            # retinas for color are assumed

            rgb_retina = False
            for name in input_names:
                if not ('Red' in name or 'Green' in name or 'Blue' in name):
                    rgb_retina=True

            if not rgb_retina:
                for name in inputs.keys():
                    r,g,b=hsv_to_rgb(features_values['hue'],1.0,1.0)
                    if (name.count('Red')):
                        inputs[name].scale=r
                    elif (name.count('Green')):
                        inputs[name].scale=g
                    elif (name.count('Blue')):
                        inputs[name].scale=b
                    else:
                        if not hasattr(self,'hue_warned'):
                            self.warning('Unable to measure hue preference, because hue is defined only when there are different input sheets with names with Red, Green or Blue substrings.')
                            self.hue_warned=True
            else:
                from contrib import rgbimages
                r,g,b=hsv_to_rgb(features_values['hue'],1.0,1.0)
                for name in inputs.keys():
                    inputs[name] = rgbimages.ExtendToRGB(generator=inputs[name],
                                                         relative_channel_strengths=[r,g,b])
                # CEBALERT: should warn as above if not a color network

        #JL: This is only used for retinotopy measurement in jude laws contrib/jsldefs.py
        #Also needs cleaned up
        if features_values.has_key('retinotopy'):
            #Calculates coordinates of the center of each patch to be presented
            coordinate_x=[]
            coordinate_y=[]
            coordinates=[]
            for name,i in zip(inputs.keys(),range(len(input_names))):
                l,b,r,t = topo.sim[name].nominal_bounds.lbrt()
                x_div=float(r-l)/(self.divisions*2)
                y_div=float(t-b)/(self.divisions*2)
                for i in range(self.divisions):
                    if not bool(self.divisions%2):
                        if bool(i%2):
                            coordinate_x.append(i*x_div)
                            coordinate_y.append(i*y_div)
                            coordinate_x.append(i*-x_div)
                            coordinate_y.append(i*-y_div)
                    else:
                        if not bool(i%2):
                            coordinate_x.append(i*x_div)
                            coordinate_y.append(i*y_div)
                            coordinate_x.append(i*-x_div)
                            coordinate_y.append(i*-y_div)
                for x in coordinate_x:
                    for y in coordinate_y:
                        coordinates.append((x,y))

                x_coord=coordinates[features_values['retinotopy']][0]
                y_coord=coordinates[features_values['retinotopy']][1]
                inputs[name].x = x_coord
                inputs[name].y = y_coord

        if features_values.has_key('retx'):
            for name,i in zip(inputs.keys(),range(len(input_names))):
                inputs[name].x = features_values['retx']
                inputs[name].y = features_values['rety']

        if features_values.has_key("phasedisparity"):
            temp_phase1=features_values['phase']-features_values['phasedisparity']/2.0
            temp_phase2=features_values['phase']+features_values['phasedisparity']/2.0
            for name in inputs.keys():
                if (name.count('Right')):
                    inputs[name].phase=wrap(0,2*pi,temp_phase1)
                elif (name.count('Left')):
                    inputs[name].phase=wrap(0,2*pi,temp_phase2)
                else:
                    if not hasattr(self,'disparity_warned'):
                        self.warning('Unable to measure disparity preference, because disparity is defined only when there are inputs for Right and Left retinas.')
                        self.disparity_warned=True

        if features_values.has_key("ocular"):
            for name in inputs.keys():
                if (name.count('Right')):
                    inputs[name].scale=2*features_values['ocular']
                elif (name.count('Left')):
                    inputs[name].scale=2.0-2*features_values['ocular']
                else:
                    self.warning('Skipping input region %s; Ocularity is defined only for Left and Right retinas.' %
                                 name)

        if features_values.has_key("contrastcenter")or param_dict.has_key("contrastcenter"):
            if self.contrast_parameter=='michelson_contrast':
                for g in inputs.itervalues():
                    g.offsetcenter=0.5
                    g.scalecenter=2*g.offsetcenter*g.contrastcenter/100.0

            elif self.contrast_parameter=='weber_contrast':
                # Weber_contrast is currently only well defined for
                # the special case where the background offset is equal
                # to the target offset in the pattern type
                # SineGrating(mask_shape=Disk())
                for g in inputs.itervalues():
                    g.offsetcenter=0.5   #In this case this is the offset of both the background and the sine grating
                    g.scalecenter=2*g.offsetcenter*g.contrastcenter/100.0

            elif self.contrast_parameter=='scale':
                for g in inputs.itervalues():
                    g.offsetcenter=0.0
                    g.scalecenter=g.contrastcenter

        if features_values.has_key("contrastsurround")or param_dict.has_key("contrastsurround"):
            if self.contrast_parameter=='michelson_contrast':
                for g in inputs.itervalues():
                    g.offsetsurround=0.5
                    g.scalesurround=2*g.offsetsurround*g.contrastsurround/100.0

            elif self.contrast_parameter=='weber_contrast':
                # Weber_contrast is currently only well defined for
                # the special case where the background offset is equal
                # to the target offset in the pattern type
                # SineGrating(mask_shape=Disk())
                for g in inputs.itervalues():
                    g.offsetsurround=0.5   #In this case this is the offset of both the background and the sine grating
                    g.scalesurround=2*g.offsetsurround*g.contrastsurround/100.0

            elif self.contrast_parameter=='scale':
                for g in inputs.itervalues():
                    g.offsetsurround=0.0
                    g.scalesurround=g.contrastsurround

        if features_values.has_key("contrast") or param_dict.has_key("contrast"):
            if self.contrast_parameter=='michelson_contrast':
                for g in inputs.itervalues():
                    g.offset=0.5
                    g.scale=2*g.offset*g.contrast/100.0

            elif self.contrast_parameter=='weber_contrast':
                # Weber_contrast is currently only well defined for
                # the special case where the background offset is equal
                # to the target offset in the pattern type
                # SineGrating(mask_shape=Disk())
                for g in inputs.itervalues():
                    g.offset=0.5   #In this case this is the offset of both the background and the sine grating
                    g.scale=2*g.offset*g.contrast/100.0

            elif self.contrast_parameter=='scale':
                for g in inputs.itervalues():
                    g.offset=0.0
                    g.scale=g.contrast

        # blank patterns for unused generator sheets
        for input_name in set(all_input_names).difference(set(input_names)):
            inputs[input_name]=pattern.Constant(scale=0)

        return inputs



class Subplotting(param.Parameterized):
    """
    Convenience functions for handling subplots (such as colorized
    Activity plots).  Only needed for avoiding typing, as plots can be
    declared with their own specific subplots without using these
    functions.
    """

    plotgroups_to_subplot=param.List(default=
        ["Activity", "Connection Fields", "Projection", "Projection Activity"],
        doc="List of plotgroups for which to set subplots.")

    subplotting_declared = param.Boolean(default=False,
        doc="Whether set_subplots has previously been called")

    _last_args = param.Parameter(default=())

    @staticmethod
    def set_subplots(prefix=None,hue="",confidence="",force=True):
        """
        Define Hue and Confidence subplots for each of the plotgroups_to_subplot.
        Typically used to make activity or weight plots show a
        preference value as the hue, and a selectivity as the
        confidence.

        The specified hue, if any, should be the name of a SheetView,
        such as OrientationPreference.  The specified confidence, if
        any, should be the name of a (usually) different SheetView,
        such as OrientationSelectivity.

        The prefix option is a shortcut making the usual case easier
        to type; it sets hue to prefix+"Preference" and confidence to
        prefix+"Selectivity".

        If force=False, subplots are changed only if no subplot is
        currently defined.  Force=False is useful for setting up
        subplots automatically when maps are measured, without
        overwriting any subplots set up specifically by the user.

        Currently works only for plotgroups that have a plot
        with the same name as the plotgroup, though this could
        be changed easily.

        Examples::

           Subplotting.set_subplots("Orientation")
             - Set the default subplots to OrientationPreference and OrientationSelectivity

           Subplotting.set_subplots(hue="OrientationPreference")
             - Set the default hue subplot to OrientationPreference with no selectivity

           Subplotting.set_subplots()
             - Remove subplots from all the plotgroups_to_subplot.
        """

        Subplotting._last_args=(prefix,hue,confidence,force)

        if Subplotting.subplotting_declared and not force:
            return

        if prefix:
            hue=prefix+"Preference"
            confidence=prefix+"Selectivity"

        for name in Subplotting.plotgroups_to_subplot:
            if plotgroups.has_key(name):
                pg=plotgroups[name]
                if pg.plot_templates.has_key(name):
                    pt=pg.plot_templates[name]
                    pt["Hue"]=hue
                    pt["Confidence"]=confidence
                else:
                    Subplotting().warning("No template %s defined for plotgroup %s" % (name,name))
            else:
                Subplotting().warning("No plotgroup %s defined" % name)

        Subplotting.subplotting_declared=True


    @staticmethod
    def restore_subplots():
        args=Subplotting._last_args
        if args != (): Subplotting.set_subplots(*(Subplotting._last_args))



class MeasureResponseCommand(ParameterizedFunction):
    """Parameterized command for presenting input patterns and measuring responses."""

    duration = param.Number(default=None,doc="""
        If non-None, pattern_presenter.duration will be
        set to this value.  Provides a simple way to set
        this commonly changed option of CoordinatedPatternGenerator.""")

    measurement_prefix = param.String(default="",doc="""
        Optional prefix to add to the name under which results are
        stored as part of a measurement response.""")

    offset = param.Number(default=0.0,softbounds=(-1.0,1.0),doc="""
        Additive offset to input pattern.""")

    pattern_coordinator = param.Callable(default=None,instantiate=True,doc="""
        Callable object that will present a parameter-controlled pattern to a
        set of Sheets.  Needs to be supplied by a subclass or in the call.
        The attributes duration and apply_output_fns (if non-None) will
        be set on this object, and it should respect those if possible.""")

    pattern_response_fn = param.Callable(default=None,instantiate=False,doc="""
        Callable object that will present a parameter-controlled pattern to a
        set of Sheets.  Needs to be supplied by a subclass or in the call.
        The attributes duration and apply_output_fns (if non-None) will
        be set on this object, and it should respect those if possible.""")

    preference_fn = param.ClassSelector(DistributionStatisticFn,default=DSF_MaxValue(),
           doc="""Function that will be used to analyze the distributions of unit responses.""")

    preference_lookup_fn = param.Callable(default=None,instantiate=True,doc="""
        Callable object that will look up a preferred feature values.""")

    scale = param.Number(default=1.0,softbounds=(0.0,2.0),doc="""
        Multiplicative strength of input pattern.""")

    static_parameters = param.List(class_=str,default=["scale","offset"],doc="""
        List of names of parameters of this class to pass to the
        pattern_presenter as static parameters, i.e. values that
        will be fixed to a single value during measurement.""")

    subplot = param.String("",doc="""Name of map to register as a subplot, if any.""")

    weighted_average= param.Boolean(default=True,doc="""
        Whether to compute results using a weighted average, or just
        discrete values.  A weighted average can give more precise
        results, without being limited to a set of discrete values,
        but the results can have systematic biases due to the
        averaging, especially for non-cyclic parameters.""")

    __abstract = True

    def __call__(self,**params):
        """Measure the response to the specified pattern and store the data in each sheet."""
        p=ParamOverrides(self,params,allow_extra_keywords=True)
        self._set_presenter_overrides(p)
        static_params = dict([(s,p[s]) for s in p.static_parameters])
        fullmatrix = FeatureMaps(self._feature_list(p),param_dict=static_params,
                                 duration=p.duration,pattern_coordinator=p.pattern_coordinator,
                                 pattern_response_fn=p.pattern_response_fn,
                                 measurement_prefix=p.measurement_prefix)

        if p.subplot != "":
            Subplotting.set_subplots(p.subplot,force=True)

        return fullmatrix


    def _feature_list(self,p):
        """Return the list of features to vary; must be implemented by each subclass."""
        raise NotImplementedError


    def _set_presenter_overrides(self,p):
        """
        Overrides parameters of the pattern_response_fn and
        pattern_coordinator, using extra_keywords passed into the
        MeasurementResponseCommand.
        """
        for override,value in p.extra_keywords().items():
            if override in p.pattern_response_fn.params():
                p.pattern_response_fn.set_param(override,value)
            if override in p.pattern_coordinator.params():
                p.pattern_coordinator.set_param(override,value)



class SinusoidalMeasureResponseCommand(MeasureResponseCommand):
    """Parameterized command for presenting sine gratings and measuring responses."""

    pattern_coordinator = param.Callable(instantiate=True,
        default=CoordinatedPatternGenerator(pattern_generator=SineGrating()),doc="""
        Callable object that will present a parameter-controlled pattern to a
        set of Sheets.  By default, uses a SineGrating presented for a short
        duration.  By convention, most Topographica example files
        are designed to have a suitable activity pattern computed by
        that time, but the duration will need to be changed for other
        models that do not follow that convention.""")

    frequencies = param.List(class_=float,default=[2.4],doc="Sine grating frequencies to test.")

    num_phase = param.Integer(default=18,bounds=(1,None),softbounds=(1,48),
                              doc="Number of phases to test.")

    num_orientation = param.Integer(default=4,bounds=(1,None),softbounds=(1,24),
                                    doc="Number of orientations to test.")

    scale = param.Number(default=0.3)

    preference_fn = param.ClassSelector(DistributionStatisticFn,default=DSF_WeightedAverage(),
            doc="""Function that will be used to analyze the distributions of unit responses.""")

    __abstract = True



class PositionMeasurementCommand(MeasureResponseCommand):
    """Parameterized command for measuring topographic position."""

    divisions=param.Integer(default=6,bounds=(1,None),doc="""
        The number of different positions to measure in X and in Y.""")

    x_range=param.NumericTuple((-0.5,0.5),doc="""
        The range of X values to test.""")

    y_range=param.NumericTuple((-0.5,0.5),doc="""
        The range of Y values to test.""")

    size=param.Number(default=0.5,bounds=(0,None),doc="""
        The size of the pattern to present.""")

    pattern_coordinator = param.Callable(
        default=CoordinatedPatternGenerator(pattern_generator=Gaussian(aspect_ratio=1.0)),doc="""
        Callable object that will present a parameter-controlled
        pattern to a set of Sheets.  For measuring position, the
        pattern_presenter should be spatially localized, yet also able
        to activate the appropriate neurons reliably.""")

    static_parameters = param.List(default=["scale","offset","size"])

    __abstract = True



class SingleInputResponseCommand(MeasureResponseCommand):
    """
    A callable Parameterized command for measuring the response to
    input on a specified Sheet.

    Note that at present the input is actually presented to all input
    sheets; the specified Sheet is simply used to determine various
    parameters.  In the future, it may be modified to draw the pattern
    on one input sheet only.
    """

    scale = param.Number(default=30.0)

    offset = param.Number(default=0.5)

    # JABALERT: Presumably the size is overridden in the call, right?
    pattern_coordinator = param.Callable(
        default=CoordinatedPatternGenerator(pattern_generator=RawRectangle(size=0.1,aspect_ratio=1.0)))

    static_parameters = param.List(default=["scale","offset","size"])

    weighted_average = None # Disabled unused parameter

    __abstract = True



class FeatureCurveCommand(SinusoidalMeasureResponseCommand):
    """A callable Parameterized command for measuring tuning curves."""

    num_orientation = param.Integer(default=12)

    units = param.String(default='%',doc="""
        Units for labeling the curve_parameters in figure legends.
        The default is %, for use with contrast, but could be any
        units (or the empty string).""")

    # Make constant in subclasses?
    x_axis = param.String(default='orientation',doc="""
        Parameter to use for the x axis of tuning curves.""")

    static_parameters = param.List(default=[])

    # JABALERT: Might want to accept a list of values for a given
    # parameter to make the simple case easier; then maybe could do
    # the crossproduct of them?
    curve_parameters=param.Parameter([{"contrast":30},{"contrast":60},{"contrast":80},{"contrast":90}],
                                     doc="""List of parameter values for which to measure a curve.""")

    __abstract = True


    def __call__(self,**params):
        """Measure the response to the specified pattern and store the data in each sheet."""
        p=ParamOverrides(self,params,allow_extra_keywords=True)
        self._set_presenter_overrides(p)
        self._compute_curves(p)


    def _compute_curves(self,p,val_format='%s'):
        """
        Compute a set of curves for the specified sheet, using the
        specified val_format to print a label for each value of a
        curve_parameter.
        """
        for curve in p.curve_parameters:
            static_params = dict([(s,p[s]) for s in p.static_parameters])
            static_params.update(curve)
            curve_label="; ".join([('%s = '+val_format+'%s') % (n.capitalize(),v,p.units) for n,v in curve.items()])
            FeatureCurves(self._feature_list(p),curve_label=curve_label,param_dict=static_params,
                          duration=p.duration,pattern_coordinator=p.pattern_coordinator,
                          pattern_response_fn=p.pattern_response_fn,x_axis=p.x_axis,
                          measurement_prefix=p.measurement_prefix)


    def _feature_list(self,p):
        return [Feature(name="phase",range=(0.0,2*pi),step=2*pi/p.num_phase,cyclic=True),
                Feature(name="orientation",range=(0,pi),step=pi/p.num_orientation,cyclic=True),
                Feature(name="frequency",values=p.frequencies)]



class UnitCurveCommand(FeatureCurveCommand):
    """
    Measures tuning curve(s) of particular unit(s).
    """

    pattern_coordinator = param.Callable(
        default=CoordinatedPatternGenerator(pattern_generator=SineGrating(mask_shape=Disk(smoothing=0.0,
                                                                                          size=1.0)),
                                            contrast_parameter="weber_contrast"))

    size=param.Number(default=0.5,bounds=(0,None),doc="""
        The size of the pattern to present.""")

    coords = param.List(default=[(0,0)],doc="""
        List of coordinates of units to measure.""")

    __abstract = True


###############################################################################
###############################################################################
###############################################################################
#
# 20081017 JABNOTE: This implementation could be improved.
#
# It currently requires every subclass to implement the feature_list
# method, which constructs a list of features using various parameters
# to determine how many and which values each feature should have.  It
# would be good to replace the feature_list method with a Parameter or
# set of Parameters, since it is simply a special data structure, and
# this would make more detailed control feasible for users. For
# instance, instead of something like num_orientations being used to
# construct the orientation Feature, the user could specify the
# appropriate Feature directly, so that they could e.g. supply a
# specific list of orientations instead of being limited to a fixed
# spacing.
#
# However, when we implemented this, we ran into two problems:
#
# 1. It's difficult for users to modify an open-ended list of
#     Features.  E.g., if features is a List:
#
#      features=param.List(doc="List of Features to vary""",default=[
#          Feature(name="frequency",values=[2.4]),
#          Feature(name="orientation",range=(0.0,pi),step=pi/4,cyclic=True),
#          Feature(name="phase",range=(0.0,2*pi),step=2*pi/18,cyclic=True)])
#
#    then it it's easy to replace the entire list, but tough to
#    change just one Feature.  Of course, features could be a
#    dictionary, but that doesn't help, because when the user
#    actually calls the function, they want the arguments to
#    affect only that call, whereas looking up the item in a
#    dictionary would only make permanent changes easy, not
#    single-call changes.
#
#    Alternatively, one could make each feature into a separate
#    parameter, and then collect them using a naming convention like:
#
#     def feature_list(self,p):
#         fs=[]
#         for n,v in self.get_param_values():
#             if n in p: v=p[n]
#             if re.match('^[^_].*_feature$',n):
#                 fs+=[v]
#         return fs
#
#    But that's quite hacky, and doesn't solve problem 2.
#
# 2. Even if the users can somehow access each Feature, the same
#    problem occurs for the individual parts of each Feature.  E.g.
#    using the separate feature parameters above, Spatial Frequency
#    map measurement would require:
#
#      from topo.command.analysis import Feature
#      from math import pi
#      pre_plot_hooks=[measure_or_pref.instance(\
#         frequency_feature=Feature(name="frequency",values=frange(1.0,6.0,0.2)), \
#         phase_feature=Feature(name="phase",range=(0.0,2*pi),step=2*pi/15,cyclic=True), \
#         orientation_feature=Feature(name="orientation",range=(0.0,pi),step=pi/4,cyclic=True)])
#
#    rather than the current, much more easily controllable implementation:
#
#      pre_plot_hooks=[measure_or_pref.instance(frequencies=frange(1.0,6.0,0.2),\
#         num_phase=15,num_orientation=4)]
#
#    I.e., to change anything about a Feature, one has to supply an
#    entirely new Feature, because otherwise the original Feature
#    would be changed for all future calls.  Perhaps there's some way
#    around this by copying objects automatically at the right time,
#    but if so it's not obvious.  Meanwhile, the current
#    implementation is reasonably clean and easy to use, if not as
#    flexible as it could be.

### PJFRALERT: Functions and classes below will stay in Topographica


def update_sheet_activity(sheet_name,sheet_views_prefix='',force=False):
    """
    Update the 'Activity' SheetView for a given sheet by name.

    If force is False and the existing Activity SheetView isn't stale,
    the existing view is returned.
    """
    sheet = topo.sim.objects(Sheet)[sheet_name]
    if not force and sheet.sheet_views.get('Activity',False):
        existing_view = sheet.sheet_views['Activity']
        if existing_view.timestamp == topo.sim.time():
            return existing_view

    updated_view =  SheetView((np.array(sheet.activity),sheet.bounds),
                              sheet.name,sheet.precedence,topo.sim.time(),sheet.row_precedence)

    sheet.sheet_views[sheet_views_prefix+'Activity'] = updated_view

    return updated_view


def update_activity(outputs={},sheet_views_prefix='',force=False):
    """
    Make a map of neural activity available for each sheet, for use in
    template-based plots.

    This command simply asks each sheet for a copy of its activity
    matrix, and then makes it available for plotting.  Of course, for
    some sheets providing this information may be non-trivial, e.g. if
    they need to average over recent spiking activity.
    """
    for sheet_name in topo.sim.objects(Sheet).keys():
        outputs[sheet_name] = update_sheet_activity(sheet_name,sheet_views_prefix,force)



def update_outputs(outputs={},sheet_views_prefix='', install_sheetview=True):
    """
    Accumulates sheet activities into the outputs dictionary, if it
    already has an entry in the dictionary. If required it will also
    install a SheetView for all sheets in the appropriate sheet_views
    dictionary.
    """
    for sheet in topo.sim.objects(Sheet).values():
        if sheet.name in outputs.keys():
            outputs[sheet.name] += sheet.activity
        if install_sheetview:
            sheet.sheet_views[sheet_views_prefix+'Activity'] = \
                SheetView((np.array(sheet.activity),sheet.bounds),sheet.name,
                          sheet.precedence,topo.sim.time(),
                          sheet.row_precedence)



class pattern_present(ParameterizedFunction):
    """
    Presents a pattern on the input sheet(s) and returns the
    response. Does not affect the state but can overwrite the previous
    pattern if overwrite_previous is set to True.

    May also be used to measure the response to a pattern by calling
    it with restore_events disabled and restore_state and
    force_sheetview enabled, which will push and pop the simulation
    state and install the response in the sheet_view dictionary. The
    measure_activity command in topo.command implements this
    functionality.

    Given a set of input patterns, installs them into the specified
    GeneratorSheets, runs the simulation for the specified length of
    time, then restores the original patterns and the original
    simulation time.  Thus this input is not considered part of the
    regular simulation, and is usually for testing purposes.

    As a special case, if 'inputs' is just a single pattern, and not
    a dictionary, it is presented to all GeneratorSheets.

    If a simulation is not provided, the active simulation, if one
    exists, is requested.

    If this process is interrupted by the user, the temporary patterns
    may still be installed on the retina.

    In order to to see the sequence of values presented, you may use
    the back arrow history mechanism in the GUI. Note that the GUI's
    Activity window must be open.
    """

    apply_output_fns=param.Boolean(default=True,doc="""
        Determines whether sheet output functions will be applied.
        """)

    duration = param.Number(default=None,doc="""
        If non-None, pattern_presenter.duration will be
        set to this value.  Provides a simple way to set
        this commonly changed option of CoordinatedPatternGenerator.""")

    inputs = param.Dict(default={},doc="""
        A dictionary of GeneratorSheetName:PatternGenerator pairs to be
        installed into the specified GeneratorSheets""")

    force_sheetview = param.Boolean(default=False,doc="""Determines
        whether to install a sheet view in the appropriate sheet_views
        dict even when there is an existing sheetview.""")

    plastic=param.Boolean(default=False,doc="""
        If plastic is False, overwrites the existing values of
        Sheet.plastic to disable plasticity, then reenables plasticity.""")

    overwrite_previous=param.Boolean(default=False,doc="""
        If overwrite_previous is true, the given inputs overwrite those
        previously defined.""")

    restore_events = param.Boolean(default=True,doc="""
        If True, restore simulation events after the response has been
        measured, so that no simulation time will have elapsed.
        Implied by restore_state=True.""")

    restore_state = param.Boolean(default=False,doc="""
        If True, restore the state of both sheet activities and simulation events
        after the response has been measured.  Implies restore_events.""")

    sheet_views_prefix = param.String(default="",doc="""
        Optional prefix to add to the name under which results are
        stored in sheet_views. Can be used e.g. to distinguish maps as
        originating from a particular GeneratorSheet.""")

    update_activity_fn = param.Callable(default=update_activity,doc="""
        Function used to update the current sheet activities.""")

    __abstract = True

    def __call__(self,inputs={},outputs={},**params_to_override):
        p=ParamOverrides(self,dict(params_to_override,inputs=inputs))
        # ensure EPs get started (if pattern_response is called before the simulation is run())
        topo.sim.run(0.0)

        if p.restore_state: topo.sim.state_push()

        if not p.overwrite_previous:
            save_input_generators()

        if not p.plastic:
            # turn off plasticity everywhere
            for sheet in topo.sim.objects(Sheet).values():
                 sheet.override_plasticity_state(new_plasticity_state=False)

        if not p.apply_output_fns:
            for each in topo.sim.objects(Sheet).values():
                if hasattr(each,'measure_maps'):
                   if each.measure_maps:
                       each.apply_output_fns = False

        # Register the inputs on each input sheet
        generatorsheets = topo.sim.objects(GeneratorSheet)

        if not isinstance(p.inputs,dict):
            for g in generatorsheets.values():
                g.set_input_generator(p.inputs)
        else:
            for each in p.inputs.keys():
                if generatorsheets.has_key(each):
                    generatorsheets[each].set_input_generator(p.inputs[each])
                else:
                    param.Parameterized().warning(
                        '%s not a valid Sheet name for pattern_present.' % each)

        if p.restore_events:  topo.sim.event_push()

        duration = p.duration if (p.duration is not None) else 1.0
        topo.sim.run(duration)

        if p.restore_events:  topo.sim.event_pop()

        # turn sheets' plasticity and output_fn plasticity back on if we turned it off before
        if not p.plastic:
            for sheet in topo.sim.objects(Sheet).values():
                sheet.restore_plasticity_state()

        if not p.apply_output_fns:
            for each in topo.sim.objects(Sheet).values():
                each.apply_output_fns = True

        if not p.overwrite_previous:
            restore_input_generators()

        p.update_activity_fn(outputs,p.sheet_views_prefix, p.force_sheetview)

        if hasattr(topo,'guimain'):
            topo.guimain.refresh_activity_windows()

        if p.restore_state: topo.sim.state_pop()

        return outputs



class MeasurementInterrupt(Exception):
    """
    Exception raised when a measurement is stopped before
    completion. Stores the number of executed presentations and
    total presentations.
    """

    def __init__(self,current,total):
        self.current = current + 1
        self.total = total + 1



class pattern_response(pattern_present):
    """
    This command is used to perform measurements, which require a
    number of permutations to complete. The inputs and outputs are
    defined as dictionaries corresponding to the generator sheets they
    are to be presented on and the measurement sheets to record from
    respectively. The update_activity_fn then accumulates the updated
    activity into the appropriate entry in the outputs dictionary.

    The command also makes sure that time, events and state are reset
    after each presentation. If a GUI is found a timer will be opened
    to display a progress bar and sheet_views will be made available
    to the sheet to display activities.
    """

    restore_state = param.Boolean(default=True,doc="""
        If True, restore the state of both sheet activities and
        simulation events after the response has been measured.
        Implies restore_events.""")

    update_activity_fn = param.Callable(default=update_outputs,doc="""
        Function used to update the current sheet activities""")

    def __call__(self,inputs={},outputs={},current=0,total=0,**params_to_override):
        if current == 0:
            self.timer = copy.copy(topo.sim.timer)
            self.timer.stop = False
            if hasattr(topo,'guimain'):
                topo.guimain.open_progress_window(self.timer)
                self.force_sheetview = True
        self.timer.update_timer('Measurement Timer',current,total)

        p=ParamOverrides(self,dict(params_to_override,inputs=inputs))

        super(pattern_response,self).__call__(outputs=outputs,**p)

        if self.timer.stop:
            raise MeasurementInterrupt(current,total)



def io_shapes(inputs=[],outputs=[]):
    """
    Return the shapes of the specified GeneratorSheets and measurement
    sheets, or if none are specified return all that can be found in
    the simulation.
    """

    input_dict = dict([(topo.sim[i].name,topo.sim[i].shape) for i in inputs
                   if i in topo.sim.objects(GeneratorSheet).values()])
    if len(input_dict.keys())<1:
        if len(inputs)>1:
            topo.sim.warning("Warning specified input sheet does not exist, using all generator sheets instead.")
        input_dict = dict((s.name, s.shape) for s in topo.sim.objects(GeneratorSheet).values())

    output_dict = dict([(topo.sim[i].name,topo.sim[i].shape) for i in outputs
                    if i in topo.sim.objects(Sheet).values()])
    if len(output_dict.keys())<1:
        if len(outputs)>1:
            topo.sim.warning("Warning specified measurement sheet does not exist, using all sheets with measure_maps enabled.")
        output_dict = dict((s.name, s.shape) for s in topo.sim.objects(Sheet).values()
                       if (hasattr(s,'measure_maps') and s.measure_maps))

    return input_dict,output_dict



def store_measurement(measurement_dict):
    """
    Interface function to install measurement results the appropriate
    sheet_views or curve_dict dictionary.
    """

    t = topo.sim.time()

    if "FeatureMaps" in measurement_dict:
        maps = measurement_dict['FeatureMaps']
        for sheet_name,map_data in maps.items():
            sheet = topo.sim[sheet_name]
            bounding_box = sheet.bounds
            sn = sheet.name
            sp = sheet.precedence
            sr = sheet.row_precedence
            for name,item in map_data.items():
                data, cyclic, cyclic_range = item
                view = SheetView((data,bounding_box),sn,sp,t,sr)
                sheet.sheet_views[name] = view
                view.cyclic = cyclic
                view.cyclic_range = cyclic_range
                sheet.sheet_views[name] = view
    elif "FeatureCurves" in measurement_dict:
        curves = measurement_dict['FeatureCurves']
        for sheet_name,curve_data in curves.items():
            sheet=topo.sim[sheet_name]
            if not hasattr(sheet,'curve_dict'):
                sheet.curve_dict = {}
            bounding_box = sheet.bounds
            sp = sheet.precedence
            sr = sheet.row_precedence
            for x_val,y_data in curve_data.items():
                prefix,curve_label,x_axis,y_axis_values = y_data
                measurement_label = prefix+x_axis
                if measurement_label not in sheet.curve_dict:
                    sheet.curve_dict[measurement_label] = {}
                if curve_label not in sheet.curve_dict[measurement_label]:
                    sheet.curve_dict[measurement_label][curve_label]={}
                view = SheetView((y_axis_values,bounding_box),sheet_name,sp,t,sr)
                sheet.curve_dict[measurement_label][curve_label].update({x_val:view})
    elif "ReverseCorrelation" in measurement_dict:
        rfs = measurement_dict['ReverseCorrelation']
        for input_name,sheet_dict in rf_dict.items():
            input_sheet = topo.sim[input_name]
            input_sheet_views = input_sheet.sheet_views
            input_bounds = input_sheet.bounds
            for sheet_name,coord_dict in sheet_dict.items():
                sheet = topo.sim[sheet_name]
                for coord,data in coord_dict.items():
                    r,c = coord
                    view = SheetView((data,input_bounds),input_sheet.name,input_sheet.precedence,
                                     topo.sim.time(),input_sheet.row_precedence)
                    x,y = sheet.matrixidx2sheet(r,c)
                    key = ('RFs',sheet_name,x,y)
                    input_sheet_views[key]=view


def get_feature_preference(feature,sheet_name,coords,default=0.0):
    """Return the feature preference for a particular unit."""
    try:
        sheet = topo.sim[sheet_name]
        matrix_coords = sheet.sheet2matrixidx(*coords)
        map_name = feature.capitalize() + "Preference"
        return sheet.sheet_views[map_name].view()[0][matrix_coords]
    except:
        topo.sim.warning(("%s should be measured before plotting this tuning curve -- " +
                          "using default value of %s for %s unit (%d,%d).") %
                         (map_name,default,sheet.name,coords[0],coords[1]))
        return default




__all__ = [
    "DistributionMatrix",
    "FullMatrix",
    "FeatureResponses",
    "ReverseCorrelation",
    "FeatureMaps",
    "FeatureCurves",
    "Feature",
    "CoordinatedPatternGenerator",
    "Subplotting",
    "MeasureResponseCommand",
    "SinusoidalMeasureResponseCommand",
    "PositionMeasurementCommand",
    "SingleInputResponseCommand",
    "FeatureCurveCommand",
    "UnitCurveCommand",
]
