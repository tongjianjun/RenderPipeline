"""

RenderPipeline

Copyright (c) 2014-2016 tobspr <tobias.springer1@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

"""

from __future__ import division

import sys
import time

from panda3d.core import LVecBase2i, TransformState, RenderState, load_prc_file
from panda3d.core import PandaSystem, WindowProperties
from panda3d.core import Vec4, Filename

from direct.showbase.ShowBase import ShowBase
from direct.stdpy.file import isfile, join

from rplibs.yaml import load_yaml_file_flat

from rpcore.globals import Globals
from rpcore.effect import Effect
from rpcore.rpobject import RPObject
from rpcore.common_resources import CommonResources
from rpcore.native import TagStateManager
from rpcore.render_target import RenderTarget
from rpcore.pluginbase.manager import PluginManager
from rpcore.pluginbase.day_manager import DayTimeManager

from rpcore.util.task_scheduler import TaskScheduler
from rpcore.util.network_communication import NetworkCommunication
from rpcore.util.ies_profile_loader import IESProfileLoader
from rpcore.util.scene_converter import SceneConverter
from rpcore.util.light_geometry import LightGeometry

from rpcore.gui.debugger import Debugger
from rpcore.gui.loading_screen import LoadingScreen

from rpcore.mount_manager import MountManager
from rpcore.stage_manager import StageManager
from rpcore.light_manager import LightManager

from rpcore.stages.ambient_stage import AmbientStage
from rpcore.stages.gbuffer_stage import GBufferStage
from rpcore.stages.final_stage import FinalStage
from rpcore.stages.convert_depth_stage import ConvertDepthStage
from rpcore.stages.combine_velocity_stage import CombineVelocityStage
from rpcore.stages.upscale_stage import UpscaleStage
from rpcore.stages.compute_low_precision_normals_stage import ComputeLowPrecisionNormalsStage
from rpcore.stages.srgb_correction_stage import SRGBCorrectionStage
from rpcore.stages.reference_stage import ReferenceStage
from rpcore.stages.menu_blur_stage import MenuBlurStage


__all__ = ("RenderPipeline")

class RenderPipeline(RPObject):

    """ This is the main render pipeline class, it combines all components of
    the pipeline to form a working system. It does not do much work itself, but
    instead setups all the managers and systems to be able to do their work. """

    def __init__(self):
        """ Creates a new pipeline with a given showbase instance. This should
        be done before intializing the ShowBase, the pipeline will take care of
        that. If the showbase has been initialized before, have a look at
        the alternative initialization of the render pipeline (the first sample)."""
        RPObject.__init__(self)
        self._analyze_system()
        self.mount_mgr = MountManager(self)
        self.settings = {}
        self._applied_effects = []
        self._stage_instances = {}
        self._pre_showbase_initialized = False
        self._rendering_enabled = True
        self._first_frame = None
        self.set_loading_screen_image("/$$rp/data/gui/loading_screen_bg.txo")

    def load_settings(self, path):
        """ Loads the pipeline configuration from a given filename. Usually
        this is the 'config/pipeline.ini' file. If you call this more than once,
        only the settings of the last file will be used. """
        self.settings = load_yaml_file_flat(path)

    def reload_shaders(self):
        """ Reloads all shaders. This will reload the shaders of all plugins,
        as well as the pipelines internally used shaders. Because of the
        complexity of some shaders, this operation take might take several
        seconds. Also notice that all applied effects will be lost, and instead
        the default effect will be set on all elements again. Due to this fact,
        this method is primarly useful for fast iterations when developing new
        shaders. """
        self.debug("Reloading shaders ..")
        if self.settings["pipeline.display_debugger"]:
            self.debugger.error_msg_handler.clear_messages()
            self.debugger.set_reload_hint_visible(True)
            # for i in range(2):
            self._showbase.graphics_engine.render_frame()

        self.tag_mgr.cleanup_states()
        self.stage_mgr.reload_shaders()
        self.light_mgr.reload_shaders()
        self.plugin_mgr.trigger_hook("shader_reload")
        if self.settings["pipeline.display_debugger"]:
            self.debugger.set_reload_hint_visible(False)
        self._apply_custom_shaders()
        self.debug("Successfully reloaded shaders.")

    def _apply_custom_shaders(self):
        """ Re-applies all custom shaders the user applied, to avoid them getting
        removed when the shaders are reloaded """
        self.debug("Re-applying", len(self._applied_effects), "custom shaders")
        for args in self._applied_effects:
            self._internal_set_effect(*args)

    def pre_showbase_init(self):
        """ Setups all required pipeline settings and configuration which have
        to be set before the showbase is setup. This is called by create(),
        in case the showbase was not initialized, however you can (and have to)
        call it manually before you init your custom showbase instance.
        See the 00-Loading the pipeline sample for more information. """
        if not self.mount_mgr.is_mounted:
            self.debug("Mount manager was not mounted, mounting ...")
            self.mount_mgr.mount()

        if not self.settings:
            self.debug("No settings loaded, loading from default location ..")
            self.load_settings("/$$rpconfig/pipeline.yaml")

        if not isfile("/$$rp/data/install.flag"):
            self.fatal("You didn't setup the pipeline yet! Please run setup.py.")

        load_prc_file("/$$rpconfig/panda3d-config.prc")
        self._pre_showbase_initialized = True

    def create(self, base=None):
        """ This creates the pipeline, and setups all buffers. It also
        constructs the showbase. The settings should have been loaded before
        calling this, and also the base and write path should have been
        initialized properly (see MountManager).

        If base is None, the showbase used in the RenderPipeline constructor
        will be used and initialized. Otherwise it is assumed that base is an
        initialized ShowBase object. In this case, you should call
        pre_showbase_init() before initializing the ShowBase"""

        start_time = time.time()
        self._init_showbase(base)

        if not self._showbase.win.gsg.supports_compute_shaders:
            self.fatal(
                "Sorry, your GPU does not support compute shaders! Make sure\n"
                "you have the latest drivers. If you already have, your gpu might\n"
                "be too old, or you might be using the open source drivers on linux.")

        self._init_globals()
        self.loading_screen.create()
        self._adjust_camera_settings()
        self._create_managers()
        self.plugin_mgr.load()
        self.daytime_mgr.load_settings()
        self.common_resources.write_config()
        self._init_debugger()

        self.plugin_mgr.trigger_hook("stage_setup")
        self.plugin_mgr.trigger_hook("post_stage_setup")

        self._create_common_defines()
        self._initialize_managers()
        self._create_default_skybox()

        self.plugin_mgr.trigger_hook("pipeline_created")

        self._listener = NetworkCommunication(self)
        self._set_default_effect()

        # Measure how long it took to initialize everything, and also store
        # when we finished, so we can measure how long it took to render the
        # first frame (where the shaders are actually compiled)
        init_duration = (time.time() - start_time)
        self._first_frame = time.clock()
        self.debug("Finished startup in {:3.3f} s".format(
            init_duration))

    def set_loading_screen_image(self, image_source):
        """ Tells the pipeline to use the default loading screen, which consists
        of a simple loading image. The image source should be a fullscreen
        16:9 image, and not too small, to avoid being blurred out. """
        self.loading_screen = LoadingScreen(self, image_source)

    def add_light(self, light):
        """ Adds a new light to the rendered lights, check out the LightManager
        add_light documentation for further information. """
        self.light_mgr.add_light(light)

    def remove_light(self, light):
        """ Removes a previously attached light, check out the LightManager
        remove_light documentation for further information. """
        self.light_mgr.remove_light(light)

    def load_ies_profile(self, filename):
        """ Loads an IES profile from a given filename and returns a handle which
        can be used to set an ies profile on a light """
        return self.ies_loader.load(filename)

    def _internal_set_effect(self, nodepath, effect_src, options=None, sort=30):
        """ Sets an effect to the given object, using the specified options.
        Check out the effect documentation for more information about possible
        options and configurations. The object should be a nodepath, and the
        effect will be applied to that nodepath and all nodepaths below whose
        current effect sort is less than the new effect sort (passed by the
        sort parameter). """
        effect = Effect.load(effect_src, options)
        if effect is None:
            return self.error("Could not apply effect")

        for i, stage in enumerate(Effect.PASSES):
            if not effect.get_option("render_" + stage):
                nodepath.hide(self.tag_mgr.get_mask(stage))
            else:
                shader = effect.get_shader_obj(stage)
                if stage == "gbuffer":
                    nodepath.set_shader(shader, 25)
                else:
                    self.tag_mgr.apply_state(
                        stage, nodepath, shader, str(effect.effect_id), 25 + 10 * i + sort)
                nodepath.show_through(self.tag_mgr.get_mask(stage))

        if effect.get_option("render_gbuffer") and effect.get_option("render_forward"):
            self.error("You cannot render an object forward and deferred at the "
                       "same time! Either use render_gbuffer or use render_forward, "
                       "but not both.")

        if effect.get_option("render_forward_prepass") and not effect.get_option("render_forward"):
            self.error("render_forward_prepass specified, but not render_forward!")

    def set_effect(self, nodepath, effect_src, options=None, sort=30):
        """ See _internal_set_effect. """
        args = (nodepath, effect_src, options, sort)
        self._applied_effects.append(args)
        self._internal_set_effect(*args)

    def add_environment_probe(self):
        """ Constructs a new environment probe and returns the handle, so that
        the probe can be modified. In case the env_probes plugin is not activated,
        this returns a dummy object which can be modified but has no impact. """
        if not self.plugin_mgr.is_plugin_enabled("env_probes"):
            self.warn("env_probes plugin is not loaded - cannot add environment probe")

            class DummyEnvironmentProbe(object):  # pylint: disable=too-few-public-methods
                def __getattr__(self, *args, **kwargs):
                    return lambda *args, **kwargs: None
            return DummyEnvironmentProbe()

        # Ugh ..
        from rpplugins.env_probes.environment_probe import EnvironmentProbe
        probe = EnvironmentProbe()
        self.plugin_mgr.instances["env_probes"].probe_mgr.add_probe(probe)
        return probe

    def prepare_scene(self, scene):
        """ Prepares a given scene. Please head over to the render pipeline wiki,
        or to util/scene_converter.py:convert for a detailed documentation on how
        this works, and what it returns. """
        return SceneConverter(self, scene).convert()

    def _create_managers(self):
        """ Internal method to create all managers and instances. This also
        initializes the commonly used render stages, which are always required,
        independently of which plugins are enabled. """
        self.task_scheduler = TaskScheduler(self)
        self.tag_mgr = TagStateManager(Globals.base.cam)
        self.plugin_mgr = PluginManager(self)
        self.stage_mgr = StageManager(self)
        self.light_mgr = LightManager(self)
        self.daytime_mgr = DayTimeManager(self)
        self.ies_loader = IESProfileLoader(self)
        self.common_resources = CommonResources(self)
        self._init_common_stages()

    def _analyze_system(self):
        """ Prints information about the system used, including information
        about the used Panda3D build. Also checks if the Panda3D build is out
        of date. """
        self.debug("Using Python {}.{} with architecture {}".format(
            sys.version_info.major, sys.version_info.minor, PandaSystem.get_platform()))
        self.debug("Using p3d{} built on {}".format(
            PandaSystem.get_version_string(), PandaSystem.get_build_date()))
        if PandaSystem.get_git_commit():
            self.debug("Using git commit {}".format(PandaSystem.get_git_commit()))
        else:
            self.debug("Using custom Panda3D build")
        if not self._check_version():
            self.fatal("Your Panda3D version is outdated! Please update to the newest \n"
                       "git version! Checkout https://github.com/panda3d/panda3d to "
                       "compile panda from source, or get a recent buildbot build.")

    def _initialize_managers(self):
        """ Internal method to initialize all managers, after they have been
        created earlier in _create_managers. The creation and initialization
        is seperated due to the fact that plugins and various other subprocesses
        have to get initialized inbetween. """
        self.stage_mgr.setup()
        self.stage_mgr.reload_shaders()
        self.light_mgr.reload_shaders()
        self._init_bindings()
        self.light_mgr.init_shadows()

    def _init_debugger(self):
        """ Internal method to initialize the GUI-based debugger. In case debugging
        is disabled, this constructs a dummy debugger, which does nothing.
        The debugger itself handles the various onscreen components. """
        if self.settings["pipeline.display_debugger"]:
            self.debugger = Debugger(self)
        else:
            # Use an empty onscreen debugger in case the debugger is not
            # enabled, which defines all member functions as empty lambdas
            class EmptyDebugger(object):  # pylint: disable=too-few-public-methods
                def __getattr__(self, *args, **kwargs):
                    return lambda *args, **kwargs: None
            self.debugger = EmptyDebugger()  # pylint: disable=redefined-variable-type
            del EmptyDebugger

    def _init_globals(self):
        """ Inits all global bindings. This includes references to the global
        ShowBase instance, as well as the render resolution, the GUI font,
        and various global logging and output methods. """
        Globals.load(self._showbase)
        native_w, native_h = self._showbase.win.get_x_size(), self._showbase.win.get_y_size()
        Globals.native_resolution = LVecBase2i(native_w, native_h)
        self._last_window_dims = LVecBase2i(Globals.native_resolution)
        self._compute_render_resolution()
        RenderTarget.RT_OUTPUT_FUNC = lambda *args: RPObject.global_warn(
            "RenderTarget", *args[1:])
        RenderTarget.USE_R11G11B10 = self.settings["pipeline.use_r11_g11_b10"]

    def _set_default_effect(self):
        """ Sets the default effect used for all objects if not overridden, this
        just calls set_effect with the default effect and options as parameters.
        This uses a very low sort, to make sure that overriding the default
        effect does not require a custom sort parameter to be passed. """
        self.set_effect(Globals.render, "effects/default.yaml", {}, -10)

    def _adjust_camera_settings(self):
        """ Sets the default camera settings, this includes the cameras
        near and far plane, as well as FoV. The reason for this is, that pandas
        default field of view is very small, and thus we increase it. """
        self._showbase.camLens.set_near_far(0.1, 70000)
        self._showbase.camLens.set_fov(40)

    def _compute_render_resolution(self):
        """ Computes the internally used render resolution. This might differ
        from the window dimensions in case a resolution scale is set. """
        scale_factor = self.settings["pipeline.resolution_scale"]
        w = int(float(Globals.native_resolution.x) * scale_factor)
        h = int(float(Globals.native_resolution.y) * scale_factor)
        # Make sure the resolution is a multiple of 4
        w, h = w - w % 4, h - h % 4
        self.debug("Render resolution is", w, "x", h)
        Globals.resolution = LVecBase2i(w, h)

    def _init_showbase(self, base):
        """ Inits the the given showbase object. This is part of an alternative
        method of initializing the showbase. In case base is None, a new
        ShowBase instance will be created and initialized. Otherwise base() is
        expected to either be an uninitialized ShowBase instance, or an
        initialized instance with pre_showbase_init() called inbefore. """
        if not base:
            self.pre_showbase_init()
            self.debug("Constructing ShowBase")
            self._showbase = ShowBase()
        else:
            if not hasattr(base, "render"):
                self.pre_showbase_init()
                self.debug("Constructing ShowBase")
                ShowBase.__init__(base)
            else:
                if not self._pre_showbase_initialized:
                    self.fatal("You constructed your own ShowBase object but you\n"
                               "did not call pre_show_base_init() on the render\n"
                               "pipeline object before! Checkout the 00-Loading the\n"
                               "pipeline sample to see how to initialize the RP.")
            self._showbase = base

        # Now that we have a showbase and a window, we can print out driver info
        self.debug("Driver Version =", self._showbase.win.gsg.driver_version)
        self.debug("Driver Vendor =", self._showbase.win.gsg.driver_vendor)
        self.debug("Driver Renderer =", self._showbase.win.gsg.driver_renderer)

    def _init_bindings(self):
        """ Internal method to init the tasks and keybindings. This constructs
        the tasks to be run on a per-frame basis. """
        self._showbase.addTask(self._manager_update_task, "RP_UpdateManagers", sort=10)
        self._showbase.addTask(self._plugin_pre_render_update, "RP_Plugin_BeforeRender", sort=12)
        self._showbase.addTask(self._plugin_post_render_update, "RP_Plugin_AfterRender", sort=15)
        self._showbase.addTask(self._update_inputs_and_stages, "RP_UpdateInputsAndStages", sort=18)
        self._showbase.addTask(self._cleanup_after_frame, "RP_CleanupAfterFrame", sort=10000)
        self._showbase.taskMgr.doMethodLater(0.5, self._clear_state_cache, "RP_ClearStateCache")
        if self.settings["pipeline.auto_reload_plugin_shaders"]:
            self._initialize_plugin_watchdog()
        self._showbase.accept("window-event", self._handle_window_event)

    def _handle_window_event(self, event):
        """ Checks for window events. This mainly handles incoming resizes,
        and calls the required handlers """
        self._showbase.windowEvent(event)
        window_dims = LVecBase2i(self._showbase.win.get_x_size(), self._showbase.win.get_y_size())
        if window_dims != self._last_window_dims and window_dims != Globals.native_resolution:
            self._last_window_dims = LVecBase2i(window_dims)

            # Ensure the dimensions are a multiple of 4, and if not, correct it
            if window_dims.x % 4 != 0 or window_dims.y % 4 != 0:
                self.debug("Correcting non-multiple of 4 window size:", window_dims)
                window_dims.x = window_dims.x - window_dims.x % 4
                window_dims.y = window_dims.y - window_dims.y % 4
                props = WindowProperties.size(window_dims.x, window_dims.y)
                self._showbase.win.request_properties(props)

            self.debug("Resizing to", window_dims.x, "x", window_dims.y)
            Globals.native_resolution = window_dims
            self._compute_render_resolution()
            self.light_mgr.compute_tile_size()
            self.stage_mgr.handle_window_resize()
            self.debugger.handle_window_resize()
            self.plugin_mgr.trigger_hook("window_resized")

    def _cleanup_after_frame(self, task=None):
        """ Task which invokes cleanup routines after the frame has been rendered """
        self.light_mgr.internal_mgr.post_render_callback()
        return task.cont

    def _clear_state_cache(self, task=None):
        """ Task which repeatedly clears the state cache to avoid storing
        unused states. While running once a while, this task prevents over-polluting
        the state-cache with unused states. This complements Panda3D's internal
        state garbarge collector, which does a great job, but still cannot clear
        up all states. """
        task.delayTime = 2.0
        TransformState.clear_cache()
        RenderState.clear_cache()
        return task.again

    def _process_plugin_reload_queue(self, task=None):
        """ Task wich processes the reload queue generated by the watchdog,
        and reloads all plugins which need to be reloaded """
        to_reload, self._queued_plugin_reloads = self._queued_plugin_reloads, set()
        for plugin_id in to_reload:
            if plugin_id not in self.plugin_mgr.instances:
                self.warn("Got invalid plugin_id:", plugin_id)
                continue
            instance = self.plugin_mgr.instances[plugin_id]
            instance.reload_shaders()
        return task.cont

    def _initialize_plugin_watchdog(self):
        """ Task which regulary checks if any shaders of a plugin got modified,
        and if so, reloads it """
        try:
            import watchdog
            import watchdog.events
            import watchdog.observers
        except ImportError:
            self.warn("File watching enabled (pipeline.auto_reload_plugin_shaders=True), "
                      "but watchdog package is not installed. Use 'ppython -m pip install watchdog' to "
                      "install it.")
            return

        self._queued_plugin_reloads = set()
        base_path = join(Filename(self.mount_mgr.base_path).to_os_specific(), "rpplugins").replace("\\", "/")
        rp_instance = self

        class EventHandler(watchdog.events.FileSystemEventHandler):
            def on_modified(self, event):
                pth = event.src_path.replace("\\", "/")
                if not pth.startswith(base_path):
                    rp_instance.error("Unkown file event on", pth)
                    return
                if not pth.endswith(".glsl"):
                    return
                pth = pth[len(base_path):]
                path_parts = pth.strip("/").split("/")
                plugin_id = path_parts[0]
                rp_instance.debug("Queuing plugin", plugin_id, "for reload, since", pth, "changed")
                rp_instance._queued_plugin_reloads.add(plugin_id)  # pylint: disable=W0212

        handler = EventHandler()
        observer = watchdog.observers.Observer()
        self.debug("Starting watchdog on", base_path)
        observer.schedule(handler, base_path, recursive=True)
        observer.start()
        self._showbase.addTask(self._process_plugin_reload_queue, "RP_ProcessPluginReloadQueue", sort=8)

    def _manager_update_task(self, task):
        """ Update task which gets called before the rendering, and updates
        all managers."""
        self.task_scheduler.step()
        self._listener.update()
        self.debugger.update()
        self.daytime_mgr.update()
        self.light_mgr.update()

        if Globals.clock.get_frame_count() == 10:
            self.debug("Initialization done. Hiding loading screen.")
            self.loading_screen.remove()

        return task.cont

    def _update_inputs_and_stages(self, task):
        """ Updates the commonly used inputs each frame. This is a seperate
        task to be able view detailed performance information in pstats, since
        a lot of matrix calculations are involved here. """
        self.common_resources.update()
        self.stage_mgr.update()
        return task.cont

    def _plugin_pre_render_update(self, task):
        """ Update task which gets called before the rendering, and updates the
        plugins. This is a seperate task to split the work, and be able to do
        better performance analysis in pstats later on. """
        self.plugin_mgr.trigger_hook("pre_render_update")
        return task.cont

    def _plugin_post_render_update(self, task):
        """ Update task which gets called after the rendering, and should cleanup
        all unused states and objects. This also triggers the plugin post-render
        update hook. """
        self.plugin_mgr.trigger_hook("post_render_update")
        if self._first_frame is not None and Globals.clock.get_frame_count() == 5:
            duration = time.clock() - self._first_frame
            self.debug("Compilation of shaders took", round(duration, 2), "seconds")
        return task.cont

    def _create_common_defines(self):
        """ Creates commonly used defines for the shader configuration. """
        defines = self.stage_mgr.defines
        defines["CAMERA_NEAR"] = round(Globals.base.camLens.get_near(), 10)
        defines["CAMERA_FAR"] = round(Globals.base.camLens.get_far(), 10)

        # Work around buggy nvidia driver, which expects arrays to be const
        if "NVIDIA 361.43" in self._showbase.win.gsg.get_driver_version():
            defines["CONST_ARRAY"] = "const"
        else:
            defines["CONST_ARRAY"] = ""

        # Provide driver vendor as a define
        vendor = self._showbase.win.gsg.get_driver_vendor().lower()
        defines["IS_NVIDIA"] = "nvidia" in vendor
        defines["IS_AMD"] = "ati" in vendor
        defines["IS_INTEL"] = "intel" in vendor

        defines["REFERENCE_MODE"] = self.settings["pipeline.reference_mode"]
        defines["HIGH_QUALITY_LIGHTING"] = self.settings["lighting.high_quality_lighting"]
        self.light_mgr.init_defines()
        self.plugin_mgr.init_defines()

    def _create_default_skybox(self, size=40000):
        """ Returns the default skybox, with a scale of <size>, and all
        proper effects and shaders already applied. The skybox is already
        parented to render as well. """
        skybox = self.common_resources.load_default_skybox()
        skybox.set_scale(size)
        skybox.reparent_to(Globals.render)
        skybox.set_bin("unsorted", 10000)
        skybox.set_name("skybox")
        self.set_effect(skybox, "effects/skybox.yaml", {
            "render_shadow": False,
            "render_envmap": False,
            "render_voxelize": False,
            "alpha_testing": False,
            "normal_mapping": False,
            "parallax_mapping": False
        }, 1000)
        return skybox

    def _check_version(self):
        """ Internal method to check if the required Panda3D version is met. Returns
        True if the version is new enough, and False if the version is outdated. """
        from panda3d.core import Texture
        if not hasattr(Texture, "F_r16i"):
            return False
        return True

    def _init_common_stages(self):
        """ Inits the commonly used stages, which don't belong to any plugin,
        but yet are necessary and widely used. """
        builtin_stages = [
            AmbientStage, GBufferStage, FinalStage, ConvertDepthStage,
            CombineVelocityStage, ComputeLowPrecisionNormalsStage,
            MenuBlurStage
        ]

        # Add an upscale/downscale stage in case we render at a different resolution
        if abs(1 - self.settings["pipeline.resolution_scale"]) > 0.005:
            builtin_stages.append(UpscaleStage)

        # Add simple SRGB stage in case we have no color correction plugin
        if not self.plugin_mgr.is_plugin_enabled("color_correction"):
            builtin_stages.append(SRGBCorrectionStage)

        if self.settings["pipeline.reference_mode"]:
            builtin_stages.append(ReferenceStage)

        for stage in builtin_stages:
            self._stage_instances[stage] = stage(self)
            self.stage_mgr.add_stage(self._stage_instances[stage])

    def _get_serialized_material_name(self, material, index=0):
        """ Returns a serializable material name """
        return str(index) + "-" + (material.get_name().replace(" ", "").strip() or "unnamed")

    def export_materials(self, pth):
        """ Exports a list of all materials found in the current scene in a
        serialized format to the given path """

        with open(pth, "w") as handle:
            for i, material in enumerate(Globals.render.find_all_materials()):
                if not material.has_base_color() or not material.has_roughness() or not material.has_refractive_index():
                    self.warn("Skipping non-pbr material '" + material.name + "'")
                    continue
                handle.write(("{} " * 11).format(
                    self._get_serialized_material_name(material, i),
                    material.base_color.x, material.base_color.y,
                    material.base_color.z, material.roughness,
                    material.refractive_index, material.metallic,
                    material.emission.x, # shading model
                    material.emission.y, # normal strength
                    material.emission.z, # arbitrary 0
                    material.emission.w, # arbitrary 1
                ) + "\n")

    def update_serialized_material(self, data):
        """ Internal method to update a material from a given serialized material """
        name = data[0]
        for i, material in enumerate(Globals.render.find_all_materials()):
            if self._get_serialized_material_name(material, i) == name:
                material.set_base_color(Vec4(float(data[1]), float(data[2]), float(data[3]), 1.0))
                material.set_roughness(float(data[4]))
                material.set_refractive_index(float(data[5]))
                material.set_metallic(float(data[6]))
                material.set_emission(Vec4(
                    float(data[7]), float(data[8]),
                    float(data[9]), float(data[10]),
                ))
                break
        else:
            self.warn("Got material update for material '" + str(name) + "' but material was not found!")
        RenderState.clear_cache()

    def add_dynamic_region(self, region):
        """ Adds a new dynamic region. All shadow sources in this region
        will be regenerated each frame. This equals calling invalidate_region
        each frame. """
        return self.light_mgr.internal_mgr.add_dynamic_region(region)

    def remove_dynamic_region(self, region):
        """ Removes a previously added dynamic region. """
        self.light_mgr.internal_mgr.remove_dynamic_region(region)

    def invalidate_region(self, region):
        """ Invalidates a region, this forces all shadows in that region
        to be updated """
        self.light_mgr.internal_mgr.invalidate_region(region)

    def make_light_geometry(self, light):
        """ Creates the appropriate geometry to represent the given light. Checkout
        the render pipeline wiki or the LightGeometry class for more information. """
        return LightGeometry.make(light)

    def enter_menu(self):
        """ Tells the render pipeline that a menu is currently open, so the render
        pipeline can pause the rendering to achive a better menu performance.
        Also applies a blur filter. """
        if not self._rendering_enabled:
            self.error("Already in menu, cannot call enter_menu again!")
            return
        self.debug("Entering menu")
        self._rendering_enabled = False
        self.stage_mgr.pause_rendering()
        self._stage_instances[MenuBlurStage].enable_blur()

    def exit_menu(self):
        """ Tells the render pipeline that the rendering can be resumed, after
        a call to enter_menu() was made """
        if self._rendering_enabled:
            self.error("Not in menu, cannot call exit_menu!")
            return
        self.debug("Leaving menu")
        self.stage_mgr.resume_rendering()
        self._stage_instances[MenuBlurStage].disable_blur()
        self._rendering_enabled = True
