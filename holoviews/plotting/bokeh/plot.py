import json
from itertools import groupby
from collections import defaultdict

import numpy as np
import param

from bokeh.models import (ColumnDataSource, Column, Row, Div)
from bokeh.models.widgets import Panel, Tabs

from ...core import (OrderedDict, Store, AdjointLayout, NdLayout, Layout,
                     Empty, GridSpace, HoloMap, Element, DynamicMap)
from ...core.options import SkipRendering
from ...core.util import basestring, wrap_tuple, unique_iterator, get_method_owner
from ...streams import Stream
from ..links import Link
from ..plot import (DimensionedPlot, GenericCompositePlot, GenericLayoutPlot,
                    GenericElementPlot, GenericOverlayPlot)
from ..util import attach_streams, displayable, collate
from .callbacks import Callback, LinkCallback
from .util import (layout_padding, pad_plots, filter_toolboxes, make_axis,
                   update_shared_sources, empty_plot, decode_bytes)

from bokeh.layouts import gridplot
from bokeh.plotting.helpers import _known_tools as known_tools
try:
    from bokeh.themes import built_in_themes
except ImportError:
    built_in_themes = {}

TOOLS = {name: tool if isinstance(tool, basestring) else type(tool())
         for name, tool in known_tools.items()}


class BokehPlot(DimensionedPlot):
    """
    Plotting baseclass for the Bokeh backends, implementing the basic
    plotting interface for Bokeh based plots.
    """

    width = param.Integer(default=300, doc="""
        Width of the plot in pixels""")

    height = param.Integer(default=300, doc="""
        Height of the plot in pixels""")

    sizing_mode = param.ObjectSelector(default='fixed',
        objects=['fixed', 'stretch_both', 'scale_width', 'scale_height',
                 'scale_both'], doc="""
        How the item being displayed should size itself.

        "stretch_both" plots will resize to occupy all available
        space, even if this changes the aspect ratio of the element.

        "fixed" plots are not responsive and will retain their
        original width and height regardless of any subsequent browser
        window resize events.

        "scale_width" elements will responsively resize to fit to the
        width available, while maintaining the original aspect ratio.

        "scale_height" elements will responsively resize to fit to the
        height available, while maintaining the original aspect ratio.

        "scale_both" elements will responsively resize to for both the
        width and height available, while maintaining the original
        aspect ratio.""")

    shared_datasource = param.Boolean(default=True, doc="""
        Whether Elements drawing the data from the same object should
        share their Bokeh data source allowing for linked brushing
        and other linked behaviors.""")

    title_format = param.String(default="{label} {group} {dimensions}", doc="""
        The formatting string for the title of this plot, allows defining
        a label group separator and dimension labels.""")

    toolbar = param.ObjectSelector(default='above',
                                   objects=["above", "below",
                                            "left", "right", None],
                                   doc="""
        The toolbar location, must be one of 'above', 'below',
        'left', 'right', None.""")

    _merged_tools = ['pan', 'box_zoom', 'box_select', 'lasso_select',
                     'poly_select', 'ypan', 'xpan']

    backend = 'bokeh'

    @property
    def document(self):
        return self._document


    @property
    def id(self):
        return self.state.ref['id']


    @document.setter
    def document(self, doc):
        self._document = doc
        if self.subplots:
            for plot in self.subplots.values():
                if plot is not None:
                    plot.document = doc


    def __init__(self, *args, **params):
        super(BokehPlot, self).__init__(*args, **params)
        self._document = None
        self.root = None


    def get_data(self, element, ranges, style):
        """
        Returns the data from an element in the appropriate format for
        initializing or updating a ColumnDataSource and a dictionary
        which maps the expected keywords arguments of a glyph to
        the column in the datasource.
        """
        raise NotImplementedError


    def _construct_callbacks(self):
        """
        Initializes any callbacks for streams which have defined
        the plotted object as a source.
        """
        if isinstance(self, GenericOverlayPlot):
            zorders = []
        elif self.batched:
            zorders = list(range(self.zorder, self.zorder+len(self.hmap.last)))
        else:
            zorders = [self.zorder]

        if isinstance(self, GenericOverlayPlot) and not self.batched:
            sources = []
        elif not self.static or isinstance(self.hmap, DynamicMap):
            sources = [(i, o) for i, inputs in self.stream_sources.items()
                       for o in inputs if i in zorders]
        else:
            sources = [(self.zorder, self.hmap.last)]

        cb_classes = set()
        for _, source in sources:
            streams = Stream.registry.get(id(source), [])
            registry = Stream._callbacks['bokeh']
            cb_classes |= {(registry[type(stream)], stream) for stream in streams
                           if type(stream) in registry and stream.linked}
        cbs = []
        sorted_cbs = sorted(cb_classes, key=lambda x: id(x[0]))
        for cb, group in groupby(sorted_cbs, lambda x: x[0]):
            cb_streams = [s for _, s in group]
            cbs.append(cb(self, cb_streams, source))
        return cbs


    def push(self):
        """
        Pushes updated plot data via the Comm.
        """
        if self.renderer.mode == 'server':
            return
        if self.comm is None:
            raise Exception('Renderer does not have a comm.')

        msg = self.renderer.diff(self, binary=True)
        if msg is None:
            return
        self.comm.send(msg.header_json)
        self.comm.send(msg.metadata_json)
        self.comm.send(msg.content_json)
        for header, payload in msg.buffers:
            self.comm.send(json.dumps(header))
            self.comm.send(buffers=[payload])


    def set_root(self, root):
        """
        Sets the current document on all subplots.
        """
        for plot in self.traverse(lambda x: x):
            plot.root = root


    def _init_datasource(self, data):
        """
        Initializes a data source to be passed into the bokeh glyph.
        """
        data = {k: decode_bytes(vs) for k, vs in data.items()}
        return ColumnDataSource(data=data)


    def _update_datasource(self, source, data):
        """
        Update datasource with data for a new frame.
        """
        data = {k: decode_bytes(vs) for k, vs in data.items()}
        empty = all(len(v) == 0 for v in data.values())
        if (self.streaming and self.streaming[0].data is self.current_frame.data
            and self._stream_data and not empty):
            stream = self.streaming[0]
            if stream._triggering:
                data = {k: v[-stream._chunk_length:] for k, v in data.items()}
                source.stream(data, stream.length)
            return

        # Determine if the CDS.data requires a full replacement or simply needs
        # to be updated. A replacement is required if untouched columns
        # are not the same length as the columns being updated.
        current_length = [len(v) for v in source.data.values() if isinstance(v, (list, np.ndarray))]
        new_length = [len(v) for v in data.values() if isinstance(v, (list, np.ndarray))]
        untouched = [k for k in source.data if k not in data]
        if (untouched and current_length and new_length and current_length[0] != new_length[0]):
            source.data = data
        else:
            source.data.update(data)

    def _update_callbacks(self, plot):
        """
        Iterates over all subplots and updates existing CustomJS
        callbacks with models that were replaced when compositing
        subplots into a CompositePlot and sets the plot id to match
        the root level bokeh model.
        """
        subplots = self.traverse(lambda x: x, [GenericElementPlot])
        merged_tools = {t: list(plot.select({'type': TOOLS[t]}))
                        for t in self._merged_tools}
        for subplot in subplots:
            for cb in subplot.callbacks:
                for c in cb.callbacks:
                    for tool, objs in merged_tools.items():
                        if tool in c.args and objs:
                            c.args[tool] = objs[0]
                    if self.top_level:
                        c.code = c.code.replace('PLACEHOLDER_PLOT_ID', self.id)

    @property
    def state(self):
        """
        The plotting state that gets updated via the update method and
        used by the renderer to generate output.
        """
        return self.handles['plot']


    @property
    def current_handles(self):
        """
        Should return a list of plot objects that have changed and
        should be updated.
        """
        return []


    def cleanup(self):
        """
        Cleans up references to the plot after the plot has been
        deleted. Traverses through all plots cleaning up Callbacks and
        Stream subscribers.
        """
        plots = self.traverse(lambda x: x, [BokehPlot])
        for plot in plots:
            if not isinstance(plot, (GenericCompositePlot, GenericElementPlot, GenericOverlayPlot)):
                continue
            streams = list(plot.streams)
            for stream in set(streams):
                stream._subscribers = [
                    (p, subscriber) for p, subscriber in stream._subscribers
                    if get_method_owner(subscriber) not in plots
                ]
            if not isinstance(plot, GenericElementPlot):
                continue
            for callback in plot.callbacks:
                streams += callback.streams
                callbacks = {k: cb for k, cb in callback._callbacks.items()
                            if cb is not callback}
                Callback._callbacks = callbacks
                callback.cleanup()


    def _fontsize(self, key, label='fontsize', common=True):
        """
        Converts integer fontsizes to a string specifying
        fontsize in pt.
        """
        size = super(BokehPlot, self)._fontsize(key, label, common)
        return {k: v if isinstance(v, basestring) else '%spt' % v
                for k, v in size.items()}


    def sync_sources(self):
        """
        Syncs data sources between Elements, which draw data
        from the same object.
        """
        get_sources = lambda x: (id(x.current_frame.data), x)
        filter_fn = lambda x: (x.shared_datasource and x.current_frame is not None and
                               not isinstance(x.current_frame.data, np.ndarray)
                               and 'source' in x.handles)
        data_sources = self.traverse(get_sources, [filter_fn])
        grouped_sources = groupby(sorted(data_sources, key=lambda x: x[0]), lambda x: x[0])
        shared_sources = []
        source_cols = {}
        plots = []
        for _, group in grouped_sources:
            group = list(group)
            if len(group) > 1:
                source_data = {}
                for _, plot in group:
                    source_data.update(plot.handles['source'].data)
                new_source = ColumnDataSource(source_data)
                for _, plot in group:
                    renderer = plot.handles.get('glyph_renderer')
                    for callback in plot.callbacks:
                        callback.reset()
                    if renderer is None:
                        continue
                    elif 'data_source' in renderer.properties():
                        renderer.update(data_source=new_source)
                    else:
                        renderer.update(source=new_source)
                    if hasattr(renderer, 'view'):
                        renderer.view.update(source=new_source)
                    plot.handles['source'] = plot.handles['cds'] = new_source
                    plots.append(plot)
                shared_sources.append(new_source)
                source_cols[id(new_source)] = [c for c in new_source.data]
        plot_id = self.id if self.top_level else None
        for plot in plots:
            for hook in plot.finalize_hooks:
                hook(plot, plot.current_frame)
            for callback in plot.callbacks:
                callback.initialize(plot_id=plot_id)
        self.handles['shared_sources'] = shared_sources
        self.handles['source_cols'] = source_cols

    def init_links(self):
        links = LinkCallback.find_links(self)
        callbacks = []
        for link, src_plot, tgt_plot in links:
            cb = Link._callbacks['bokeh'][type(link)]
            callbacks.append(cb(self, link, src_plot, tgt_plot))
        return callbacks



class CompositePlot(BokehPlot):
    """
    CompositePlot is an abstract baseclass for plot types that draw
    render multiple axes. It implements methods to add an overall title
    to such a plot.
    """

    fontsize = param.Parameter(default={'title': '15pt'}, allow_None=True,  doc="""
       Specifies various fontsizes of the displayed text.

       Finer control is available by supplying a dictionary where any
       unmentioned keys reverts to the default sizes, e.g:

          {'title': '15pt'}""")

    _title_template = (
        '<span style='
        '"color:{color};font-family:{font};'
        'font-style:{fontstyle};font-weight:{fontstyle};'  # italic/bold
        'font-size:{fontsize}">'
        '{title}</span>'
    )

    def _get_title(self, key):
        title_div = None
        title = self._format_title(key) if self.show_title else ''
        if not title:
            return title_div

        try:
            title_json = (built_in_themes[self.renderer.theme]
                          ._json['attrs'].get('Title', {}))
        except KeyError:
            title_json = {}

        color = title_json.get('text_color', None)
        font = title_json.get('text_font', 'Arial')
        fontstyle = title_json.get('text_font_style', 'bold')

        fontsize = self._fontsize('title')['fontsize']
        if fontsize == '15pt':
            fontsize = title_json.get('text_font_size', '15pt')
            if 'em' in fontsize:
                # it's smaller than it shosuld be so add 0.25
                fontsize = str(float(fontsize[:-2]) + 0.25) + 'em'

        title_tags = self._title_template.format(
            color=color,
            font=font,
            fontstyle=fontstyle,
            fontsize=fontsize,
            title=title)

        if 'title' in self.handles:
            title_div = self.handles['title']
        else:
            title_div = Div(width=450)  # so it won't wrap long titles easily
        title_div.text = title_tags

        return title_div

    @property
    def current_handles(self):
        """
        Should return a list of plot objects that have changed and
        should be updated.
        """
        return [self.handles['title']] if 'title' in self.handles else []



class GridPlot(CompositePlot, GenericCompositePlot):
    """
    Plot a group of elements in a grid layout based on a GridSpace element
    object.
    """

    axis_offset = param.Integer(default=50, doc="""
        Number of pixels to adjust row and column widths and height by
        to compensate for shared axes.""")

    fontsize = param.Parameter(default={'title': '16pt'},
                               allow_None=True,  doc="""
       Specifies various fontsizes of the displayed text.

       Finer control is available by supplying a dictionary where any
       unmentioned keys reverts to the default sizes, e.g:

          {'title': '15pt'}""")

    merge_tools = param.Boolean(default=True, doc="""
        Whether to merge all the tools into a single toolbar""")

    shared_xaxis = param.Boolean(default=False, doc="""
        If enabled the x-axes of the GridSpace will be drawn from the
        objects inside the Grid rather than the GridSpace dimensions.""")

    shared_yaxis = param.Boolean(default=False, doc="""
        If enabled the x-axes of the GridSpace will be drawn from the
        objects inside the Grid rather than the GridSpace dimensions.""")

    xaxis = param.ObjectSelector(default=True,
                                 objects=['bottom', 'top', None, True, False], doc="""
        Whether and where to display the xaxis, supported options are
        'bottom', 'top' and None.""")

    yaxis = param.ObjectSelector(default=True,
                                 objects=['left', 'right', None, True, False], doc="""
        Whether and where to display the yaxis, supported options are
        'left', 'right' and None.""")

    xrotation = param.Integer(default=0, bounds=(0, 360), doc="""
        Rotation angle of the xticks.""")

    yrotation = param.Integer(default=0, bounds=(0, 360), doc="""
        Rotation angle of the yticks.""")

    plot_size = param.Integer(default=120, doc="""
        Defines the width and height of each plot in the grid""")

    def __init__(self, layout, ranges=None, layout_num=1, keys=None, **params):
        if not isinstance(layout, GridSpace):
            raise Exception("GridPlot only accepts GridSpace.")
        super(GridPlot, self).__init__(layout=layout, layout_num=layout_num,
                                       ranges=ranges, keys=keys, **params)
        self.cols, self.rows = layout.shape
        self.subplots, self.layout = self._create_subplots(layout, ranges)
        if self.top_level:
            self.comm = self.init_comm()
            self.traverse(lambda x: setattr(x, 'comm', self.comm))
            self.traverse(lambda x: attach_streams(self, x.hmap, 2),
                          [GenericElementPlot])

    def _create_subplots(self, layout, ranges):
        subplots = OrderedDict()
        frame_ranges = self.compute_ranges(layout, None, ranges)
        keys = self.keys[:1] if self.dynamic else self.keys
        frame_ranges = OrderedDict([(key, self.compute_ranges(layout, key, frame_ranges))
                                    for key in keys])
        collapsed_layout = layout.clone(shared_data=False, id=layout.id)
        for i, coord in enumerate(layout.keys(full_grid=True)):
            r = i % self.rows
            c = i // self.rows

            if not isinstance(coord, tuple): coord = (coord,)
            view = layout.data.get(coord, None)
            # Create subplot
            if view is not None:
                vtype = view.type if isinstance(view, HoloMap) else view.__class__
                opts = self.lookup_options(view, 'plot').options
            else:
                vtype = None

            if type(view) in (Layout, NdLayout):
                raise SkipRendering("Cannot plot nested Layouts.")
            if not displayable(view):
                view = collate(view)

            # Create axes
            offset = self.axis_offset
            kwargs = {}
            if c == 0 and r != 0:
                kwargs['xaxis'] = None
                kwargs['width'] = self.plot_size+offset
            if c != 0 and r == 0:
                kwargs['yaxis'] = None
                kwargs['height'] = self.plot_size+offset
            if c == 0 and r == 0:
                kwargs['width'] = self.plot_size+offset
                kwargs['height'] = self.plot_size+offset
            if r != 0 and c != 0:
                kwargs['xaxis'] = None
                kwargs['yaxis'] = None

            if 'width' not in kwargs or not self.shared_yaxis:
                kwargs['width'] = self.plot_size
            if 'height' not in kwargs or not self.shared_xaxis:
                kwargs['height'] = self.plot_size
            if 'border' not in kwargs:
                kwargs['border'] = 3

            kwargs['show_legend'] = False

            if not self.shared_xaxis:
                kwargs['xaxis'] = None

            if not self.shared_yaxis:
                kwargs['yaxis'] = None

            # Create subplot
            plotting_class = Store.registry[self.renderer.backend].get(vtype, None)
            if plotting_class is None:
                if view is not None:
                    self.warning("Bokeh plotting class for %s type not found, "
                                 "object will not be rendered." % vtype.__name__)
            else:
                subplot = plotting_class(view, dimensions=self.dimensions,
                                         show_title=False, subplot=True,
                                         renderer=self.renderer,
                                         ranges=frame_ranges, uniform=self.uniform,
                                         keys=self.keys, **dict(opts, **kwargs))
                collapsed_layout[coord] = (subplot.layout
                                           if isinstance(subplot, GenericCompositePlot)
                                           else subplot.hmap)
                subplots[coord] = subplot
        return subplots, collapsed_layout


    def initialize_plot(self, ranges=None, plots=[]):
        ranges = self.compute_ranges(self.layout, self.keys[-1], None)
        passed_plots = list(plots)
        plots = [[None for c in range(self.cols)] for r in range(self.rows)]
        for i, coord in enumerate(self.layout.keys(full_grid=True)):
            r = i % self.rows
            c = i // self.rows
            subplot = self.subplots.get(wrap_tuple(coord), None)
            if subplot is not None:
                plot = subplot.initialize_plot(ranges=ranges, plots=passed_plots)
                plots[r][c] = plot
                passed_plots.append(plot)
            else:
                passed_plots.append(None)

        plot = gridplot(plots[::-1], toolbar_location=self.toolbar,
                        merge_tools=self.merge_tools)
        plot = self._make_axes(plot)

        title = self._get_title(self.keys[-1])
        if title:
            plot = Column(title, plot)
            self.handles['title'] = title

        self.handles['plot'] = plot
        self.handles['plots'] = plots

        self._update_callbacks(plot)
        if self.shared_datasource:
            self.sync_sources()

        if self.top_level:
            self.init_links()

        self.drawn = True

        return self.handles['plot']


    def _make_axes(self, plot):
        width, height = self.renderer.get_size(plot)
        x_axis, y_axis = None, None
        kwargs = dict(sizing_mode=self.sizing_mode)
        keys = self.layout.keys(full_grid=True)
        if self.xaxis:
            flip = self.shared_xaxis
            rotation = self.xrotation
            lsize = self._fontsize('xlabel').get('fontsize')
            tsize = self._fontsize('xticks', common=False).get('fontsize')
            xfactors = list(unique_iterator([wrap_tuple(k)[0] for k in keys]))
            x_axis = make_axis('x', width, xfactors, self.layout.kdims[0],
                               flip=flip, rotation=rotation, label_size=lsize,
                               tick_size=tsize)
        if self.yaxis and self.layout.ndims > 1:
            flip = self.shared_yaxis
            rotation = self.yrotation
            lsize = self._fontsize('ylabel').get('fontsize')
            tsize = self._fontsize('yticks', common=False).get('fontsize')
            yfactors = list(unique_iterator([k[1] for k in keys]))
            y_axis = make_axis('y', height, yfactors, self.layout.kdims[1],
                               flip=flip, rotation=rotation, label_size=lsize,
                               tick_size=tsize)
        if x_axis and y_axis:
            plot = filter_toolboxes(plot)
            r1, r2 = ([y_axis, plot], [None, x_axis])
            if self.shared_xaxis:
                r1, r2 = r2, r1
            if self.shared_yaxis:
                r1, r2 = r1[::-1], r2[::-1]
            models = layout_padding([r1, r2], self.renderer)
            plot = gridplot(models, **kwargs)
        elif y_axis:
            models = [y_axis, plot]
            if self.shared_yaxis: models = models[::-1]
            plot = Row(*models, **kwargs)
        elif x_axis:
            models = [plot, x_axis]
            if self.shared_xaxis: models = models[::-1]
            plot = Column(*models, **kwargs)
        return plot


    @update_shared_sources
    def update_frame(self, key, ranges=None):
        """
        Update the internal state of the Plot to represent the given
        key tuple (where integers represent frames). Returns this
        state.
        """
        ranges = self.compute_ranges(self.layout, key, ranges)
        for coord in self.layout.keys(full_grid=True):
            subplot = self.subplots.get(wrap_tuple(coord), None)
            if subplot is not None:
                subplot.update_frame(key, ranges)
        title = self._get_title(key)
        if title:
            self.handles['title']



class LayoutPlot(CompositePlot, GenericLayoutPlot):

    shared_axes = param.Boolean(default=True, doc="""
        Whether axes should be shared across plots""")

    shared_datasource = param.Boolean(default=False, doc="""
        Whether Elements drawing the data from the same object should
        share their Bokeh data source allowing for linked brushing
        and other linked behaviors.""")

    merge_tools = param.Boolean(default=True, doc="""
        Whether to merge all the tools into a single toolbar""")

    tabs = param.Boolean(default=False, doc="""
        Whether to display overlaid plots in separate panes""")

    def __init__(self, layout, keys=None, **params):
        super(LayoutPlot, self).__init__(layout, keys=keys, **params)
        self.layout, self.subplots, self.paths = self._init_layout(layout)
        if self.top_level:
            self.comm = self.init_comm()
            self.traverse(lambda x: setattr(x, 'comm', self.comm))
            self.traverse(lambda x: attach_streams(self, x.hmap, 2),
                          [GenericElementPlot])

    def _init_layout(self, layout):
        # Situate all the Layouts in the grid and compute the gridspec
        # indices for all the axes required by each LayoutPlot.
        layout_count = 0
        collapsed_layout = layout.clone(shared_data=False, id=layout.id)
        frame_ranges = self.compute_ranges(layout, None, None)
        keys = self.keys[:1] if self.dynamic else self.keys
        frame_ranges = OrderedDict([(key, self.compute_ranges(layout, key, frame_ranges))
                                    for key in keys])
        layout_items = layout.grid_items()
        layout_dimensions = layout.kdims if isinstance(layout, NdLayout) else None
        layout_subplots, layouts, paths = {}, {}, {}
        for r, c in self.coords:
            # Get view at layout position and wrap in AdjointLayout
            key, view = layout_items.get((c, r) if self.transpose else (r, c), (None, None))
            view = view if isinstance(view, AdjointLayout) else AdjointLayout([view])
            layouts[(r, c)] = view
            paths[r, c] = key

            # Compute the layout type from shape
            layout_lens = {1:'Single', 2:'Dual', 3: 'Triple'}
            layout_type = layout_lens.get(len(view), 'Single')

            # Get the AdjoinLayout at the specified coordinate
            positions = AdjointLayoutPlot.layout_dict[layout_type]['positions']

            # Create temporary subplots to get projections types
            # to create the correct subaxes for all plots in the layout
            layout_key, _ = layout_items.get((r, c), (None, None))
            if isinstance(layout, NdLayout) and layout_key:
                layout_dimensions = OrderedDict(zip(layout_dimensions, layout_key))

            # Generate the axes and create the subplots with the appropriate
            # axis objects, handling any Empty objects.
            empty = isinstance(view.main, Empty)
            if empty or view.main is None:
                continue
            elif not view.traverse(lambda x: x, [Element]):
                self.warning('%s is empty, skipping subplot.' % view.main)
                continue
            else:
                layout_count += 1
            num = 0 if empty else layout_count
            subplots, adjoint_layout = self._create_subplots(
                view, positions, layout_dimensions, frame_ranges, num=num
            )

            # Generate the AdjointLayoutsPlot which will coordinate
            # plotting of AdjointLayouts in the larger grid
            plotopts = self.lookup_options(view, 'plot').options
            layout_plot = AdjointLayoutPlot(adjoint_layout, layout_type, subplots, **plotopts)
            layout_subplots[(r, c)] = layout_plot
            if layout_key:
                collapsed_layout[layout_key] = adjoint_layout
        return collapsed_layout, layout_subplots, paths


    def _create_subplots(self, layout, positions, layout_dimensions, ranges, num=0):
        """
        Plot all the views contained in the AdjointLayout Object using axes
        appropriate to the layout configuration. All the axes are
        supplied by LayoutPlot - the purpose of the call is to
        invoke subplots with correct options and styles and hide any
        empty axes as necessary.
        """
        subplots = {}
        adjoint_clone = layout.clone(shared_data=False, id=layout.id)
        main_plot = None
        for pos in positions:
            # Pos will be one of 'main', 'top' or 'right' or None
            element = layout.get(pos, None)
            if element is None or not element.traverse(lambda x: x, [Element, Empty]):
                continue
            if not displayable(element):
                element = collate(element)

            subplot_opts = dict(adjoined=main_plot)
            # Options common for any subplot
            vtype = element.type if isinstance(element, HoloMap) else element.__class__
            plot_type = Store.registry[self.renderer.backend].get(vtype, None)
            plotopts = self.lookup_options(element, 'plot').options
            side_opts = {}
            if pos != 'main':
                plot_type = AdjointLayoutPlot.registry.get(vtype, plot_type)
                if pos == 'right':
                    yaxis = 'right-bare' if plot_type and 'bare' in plot_type.yaxis else 'right'
                    width = plot_type.width if plot_type else 0
                    side_opts = dict(height=main_plot.height, yaxis=yaxis,
                                     width=width, invert_axes=True,
                                     labelled=['y'], xticks=1, xaxis=main_plot.xaxis)
                else:
                    xaxis = 'top-bare' if plot_type and 'bare' in plot_type.xaxis else 'top'
                    height = plot_type.height if plot_type else 0
                    side_opts = dict(width=main_plot.width, xaxis=xaxis,
                                     height=height, labelled=['x'],
                                     yticks=1, yaxis=main_plot.yaxis)

            # Override the plotopts as required
            # Customize plotopts depending on position.
            plotopts = dict(side_opts, **plotopts)
            plotopts.update(subplot_opts)

            if vtype is Empty:
                adjoint_clone[pos] = element
                subplots[pos] = None
                continue
            elif plot_type is None:
                self.warning("Bokeh plotting class for %s type not found, object will "
                             "not be rendered." % vtype.__name__)
                continue
            num = num if len(self.coords) > 1 else 0
            subplot = plot_type(element, keys=self.keys,
                                dimensions=self.dimensions,
                                layout_dimensions=layout_dimensions,
                                ranges=ranges, subplot=True,
                                uniform=self.uniform, layout_num=num,
                                renderer=self.renderer,
                                **dict({'shared_axes': self.shared_axes},
                                       **plotopts))
            subplots[pos] = subplot
            if isinstance(plot_type, type) and issubclass(plot_type, GenericCompositePlot):
                adjoint_clone[pos] = subplots[pos].layout
            else:
                adjoint_clone[pos] = subplots[pos].hmap
            if pos == 'main':
                main_plot = subplot

        return subplots, adjoint_clone


    def _compute_grid(self):
        """
        Computes an empty grid to position the plots on by expanding
        any AdjointLayouts into multiple rows and columns.
        """
        widths = []
        for c in range(self.cols):
            c_widths = []
            for r in range(self.rows):
                subplot = self.subplots.get((r, c), None)
                nsubplots = 1 if subplot is None else len(subplot.layout)
                c_widths.append(2 if nsubplots > 1 else 1)
            widths.append(max(c_widths))

        heights = []
        for r in range(self.rows):
            r_heights = []
            for c in range(self.cols):
                subplot = self.subplots.get((r, c), None)
                nsubplots = 1 if subplot is None else len(subplot.layout)
                r_heights.append(2 if nsubplots > 2 else 1)
            heights.append(max(r_heights))

        # Generate empty grid
        rows = sum(heights)
        cols = sum(widths)
        grid = [[None]*cols for _ in range(rows)]

        return grid


    def initialize_plot(self, plots=None, ranges=None):
        ranges = self.compute_ranges(self.layout, self.keys[-1], None)

        plot_grid = self._compute_grid()
        passed_plots = [] if plots is None else plots
        r_offset = 0
        col_offsets = defaultdict(int)
        tab_plots = []

        for r in range(self.rows):
            # Compute row offset
            row = [(k, sp) for k, sp in self.subplots.items() if k[0] == r]
            row_padded = any(len(sp.layout) > 2 for k, sp in row)
            if row_padded:
                r_offset += 1

            for c in range(self.cols):
                subplot = self.subplots.get((r, c), None)

                # Compute column offset
                col = [(k, sp) for k, sp in self.subplots.items() if k[1] == c]
                col_padded = any(len(sp.layout) > 1 for k, sp in col)
                if col_padded:
                    col_offsets[r] += 1
                c_offset = col_offsets.get(r, 0)

                if subplot is None:
                    continue

                shared_plots = list(passed_plots) if self.shared_axes else None
                subplots = subplot.initialize_plot(ranges=ranges, plots=shared_plots)
                nsubplots = len(subplots)

                # If tabs enabled lay out AdjointLayout on grid
                if self.tabs:
                    title = subplot.subplots['main']._format_title(self.keys[-1],
                                                                   dimensions=False)
                    if not title:
                        title = ' '.join(self.paths[r,c])
                    if nsubplots == 1:
                        grid = subplots[0]
                    elif nsubplots == 2:
                        grid = gridplot([subplots], merge_tools=self.merge_tools,
                                        toolbar_location=self.toolbar)
                    else:
                        grid = [[subplots[2], None], subplots[:2]]
                        grid = gridplot(children=grid, merge_tools=self.merge_tools,
                                        toolbar_location=self.toolbar)
                    tab_plots.append((title, grid))
                    continue

                # Situate plot in overall grid
                if nsubplots > 2:
                    plot_grid[r+r_offset-1][c+c_offset-1] = subplots[2]
                plot_column = plot_grid[r+r_offset]
                if nsubplots > 1:
                    plot_column[c+c_offset-1] = subplots[0]
                    plot_column[c+c_offset] = subplots[1]
                else:
                    plot_column[c+c_offset-int(col_padded)] = subplots[0]
                passed_plots.append(subplots[0])

        # Wrap in appropriate layout model
        kwargs = dict(sizing_mode=self.sizing_mode)
        if self.tabs:
            plots = filter_toolboxes([p for t, p in tab_plots])
            panels = [Panel(child=child, title=t) for t, child in tab_plots]
            layout_plot = Tabs(tabs=panels)
        else:
            plot_grid = layout_padding(plot_grid, self.renderer)
            plot_grid = filter_toolboxes(plot_grid)
            plot_grid = pad_plots(plot_grid)
            layout_plot = gridplot(children=plot_grid,
                                   toolbar_location=self.toolbar,
                                   merge_tools=self.merge_tools, **kwargs)

        title = self._get_title(self.keys[-1])
        if title:
            self.handles['title'] = title
            layout_plot = Column(title, layout_plot, **kwargs)

        self.handles['plot'] = layout_plot
        self.handles['plots'] = plots

        self._update_callbacks(layout_plot)
        if self.shared_datasource:
            self.sync_sources()

        if self.top_level:
            self.init_links()

        self.drawn = True

        return self.handles['plot']

    @update_shared_sources
    def update_frame(self, key, ranges=None):
        """
        Update the internal state of the Plot to represent the given
        key tuple (where integers represent frames). Returns this
        state.
        """
        ranges = self.compute_ranges(self.layout, key, ranges)
        for r, c in self.coords:
            subplot = self.subplots.get((r, c), None)
            if subplot is not None:
                subplot.update_frame(key, ranges)
        title = self._get_title(key)
        if title:
            self.handles['title'] = title



class AdjointLayoutPlot(BokehPlot):

    layout_dict = {'Single': {'positions': ['main']},
                   'Dual':   {'positions': ['main', 'right']},
                   'Triple': {'positions': ['main', 'right', 'top']}}

    registry = {}

    def __init__(self, layout, layout_type, subplots, **params):
        # The AdjointLayout ViewableElement object
        self.layout = layout
        # Type may be set to 'Embedded Dual' by a call it grid_situate
        self.layout_type = layout_type
        self.view_positions = self.layout_dict[self.layout_type]['positions']

        # The supplied (axes, view) objects as indexed by position
        super(AdjointLayoutPlot, self).__init__(subplots=subplots, **params)


    def initialize_plot(self, ranges=None, plots=[]):
        """
        Plot all the views contained in the AdjointLayout Object using axes
        appropriate to the layout configuration. All the axes are
        supplied by LayoutPlot - the purpose of the call is to
        invoke subplots with correct options and styles and hide any
        empty axes as necessary.
        """
        if plots is None: plots = []
        adjoined_plots = []
        for pos in self.view_positions:
            # Pos will be one of 'main', 'top' or 'right' or None
            subplot = self.subplots.get(pos, None)
            # If no view object or empty position, disable the axis
            if subplot is None:
                adjoined_plots.append(empty_plot(0, 0))
            else:
                passed_plots = plots + adjoined_plots
                adjoined_plots.append(subplot.initialize_plot(ranges=ranges, plots=passed_plots))
        self.drawn = True
        if not adjoined_plots: adjoined_plots = [None]
        return adjoined_plots


    def update_frame(self, key, ranges=None):
        plot = None
        for pos in ['main', 'right', 'top']:
            subplot = self.subplots.get(pos)
            if subplot is not None:
                plot = subplot.update_frame(key, ranges)
        return plot
