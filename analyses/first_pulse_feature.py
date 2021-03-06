import pyqtgraph as pg
import numpy as np
import csv
import sys
import argparse
from multipatch_analysis.experiment_list import cached_experiments
from manuscript_figures import get_response, get_amplitude, response_filter, feature_anova, write_cache, trace_plot, \
    colors_human, colors_mouse, fail_rate, pulse_qc
from synapse_comparison import load_cache, summary_plot_pulse
from neuroanalysis.data import TraceList
from neuroanalysis.ui.plot_grid import PlotGrid
from multipatch_analysis.connection_detection import fit_psp
from rep_connections import ee_connections, human_connections, no_include, all_connections, ie_connections, ii_connections, ei_connections
from multipatch_analysis.synaptic_dynamics import DynamicsAnalyzer

app = pg.mkQApp()
pg.dbg()
pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')

parser = argparse.ArgumentParser(description='Enter organism and type of connection you"d like to analyze ex: mouse ee (all mouse excitatory-'
                'excitatory). Alternatively enter a cre-type connection ex: sim1-sim1')
parser.add_argument('--organism', dest='organism', help='Select mouse or human')
parser.add_argument('--connection', dest='connection', help='Specify connections to analyze')
args = vars(parser.parse_args(sys.argv[1:]))

all_expts = cached_experiments()

if args['organism'] == 'mouse':
    color_palette = colors_mouse
    calcium = 'high'
    age = '40-60'
    connection = args['connection']
    if connection == 'ee':
        connection_types = ee_connections.keys()
    elif connection == 'ii':
        connection_types = ii_connections.keys()
    elif connection == 'ei':
        connection_types = ei_connections.keys()
    elif connection == 'ie':
        connection_types == ie_connections.keys()
    elif connection == 'all':
        connection_types = all_connections.keys()
    elif len(connection.split('-')) == 2:
        c_type = connection.split('-')
        if c_type[0] == '2/3':
            pre_type = ('2/3', 'unknown')
        else:
            pre_type = (None, c_type[0])
        if c_type[1] == '2/3':
            post_type = ('2/3', 'unknown')
        else:
            post_type = (None, c_type[0])
        connection_types = [(pre_type, post_type)]
elif args['organism'] == 'human':
    color_palette = colors_human
    calcium = None
    age = None
    connection = args['connection']
    if connection == 'ee':
        connection_types = human_connections.keys()
    else:
        c_type = connection.split('-')
        connection_types = [((c_type[0], 'unknown'), (c_type[1], 'unknown'))]

sweep_threshold = 5
threshold = 0.03e-3
scale_offset = (-20, -20)
scale_anchor = (0.4, 1)
qc_plot = pg.plot()
grand_response = {}
feature_plot = None
synapse_plot = PlotGrid()
synapse_plot.set_shape(len(connection_types), 1)
synapse_plot.show()
for c in range(len(connection_types)):
    cre_type = (connection_types[c][0][1], connection_types[c][1][1])
    target_layer = (connection_types[c][0][0], connection_types[c][1][0])
    type = connection_types[c]
    expt_list = all_expts.select(cre_type=cre_type, target_layer=target_layer, calcium=calcium, age=age)
    color = color_palette[c]
    grand_response[type[0]] = {'trace': [], 'amp': [], 'latency': [], 'rise': [], 'decay':[], 'fail_rate': []}
    synapse_plot[c, 0].addLegend()
    for expt in expt_list:
        for pre, post in expt.connections:
            if [expt.uid, pre, post] in no_include:
                continue
            cre_check = expt.cells[pre].cre_type == cre_type[0] and expt.cells[post].cre_type == cre_type[1]
            layer_check = expt.cells[pre].target_layer == target_layer[0] and expt.cells[post].target_layer == target_layer[1]
            if cre_check is True and layer_check is True:
                pulse_response, artifact = get_response(expt, pre, post, type='pulse')
                if threshold is not None and artifact > threshold:
                    continue
                response_subset = response_filter(pulse_response, freq_range=[0, 50], holding_range=[-68, -72], pulse=True)
                if len(response_subset) >= sweep_threshold:
                    qc_plot.clear()
                    qc_list = pulse_qc(response_subset, baseline=2.5, pulse=None, plot=qc_plot)
                    if len(qc_list) >= sweep_threshold:
                        avg_trace, avg_amp, amp_sign, peak_t = get_amplitude(qc_list)
                        if amp_sign is '-':
                            continue
                        #grand_response[cre_type[0]]['fail_rate'].append(fail_rate(response_subset, '+', peak_t))
                        psp_fits = fit_psp(avg_trace, sign=amp_sign, yoffset=0, amp=avg_amp, method='leastsq', fit_kws={})
                        avg_trace.t0 = -(psp_fits.best_values['xoffset'] - 10e-3)
                        grand_response[type[0]]['latency'].append(psp_fits.best_values['xoffset'] - 10e-3)
                        grand_response[type[0]]['rise'].append(psp_fits.best_values['rise_time'])
                        grand_response[type[0]]['trace'].append(avg_trace)
                        grand_response[type[0]]['amp'].append(avg_amp)
                        synapse_plot[c, 0].setTitle('First Pulse Response')
                        if [expt.uid, pre, post] == all_connections[type]:
                            trace_color = (255, 0, 255, 30)
                        else:
                            trace_color = (0, 0, 0, 30)
                        trace_plot(avg_trace, trace_color, plot=synapse_plot[c, 0], x_range=[0, 27e-3])
                        app.processEvents()
                    decay_response = response_filter(pulse_response, freq_range=[0, 20], holding_range=[-68, -72])
                    qc_list = pulse_qc(response_subset, baseline=2, pulse=None, plot=qc_plot)
                    if len(qc_list) >= sweep_threshold:
                        avg_trace, avg_amp, amp_sign, peak_t = get_amplitude(qc_list)
                        if amp_sign is '-':
                            continue
                        psp_fits = fit_psp(avg_trace, sign=amp_sign, yoffset=0, amp=avg_amp, method='leastsq', fit_kws={})
                        grand_response[type[0]]['decay'].append(psp_fits.best_values['decay_tau'])
    if len(grand_response[type[0]]['trace']) == 0:
        continue
    if len(grand_response[type[0]]['trace']) > 1:
        grand_trace = TraceList(grand_response[type[0]]['trace']).mean()
        grand_trace.t0 = 0
    else:
        grand_trace = grand_response[type[0]]['trace'][0]
    n_synapses = len(grand_response[type[0]]['trace'])
    trace_plot(grand_trace, color={'color': color, 'width': 2}, plot=synapse_plot[c, 0], x_range=[0, 27e-3],
               name=('%s, n = %d' % (connection_types[c], n_synapses)))
    synapse_plot[c, 0].hideAxis('bottom')
    # all_amps = np.hstack(np.asarray(grand_response[cre_type[0]]['fail_rate']))
    # y, x = np.histogram(all_amps, bins=np.linspace(0, 2e-3, 40))
    # synapse_plot[c, 1].plot(x, y, stepMode=True, fillLevel=0, brush='k')
    # synapse_plot[c, 1].setLabels(bottom=('Vm', 'V'))
    # synapse_plot[c, 1].setXRange(0, 2e-3)
    feature_list = (grand_response[type[0]]['amp'], grand_response[type[0]]['latency'], grand_response[type[0]]['rise'],
                    grand_response[type[0]]['decay'])
    grand_amp = np.mean(np.array(grand_response[type[0]]['amp']))
    grand_latency = np.mean(np.array(grand_response[type[0]]['latency']))
    grand_rise = np.mean(np.array(grand_response[type[0]]['rise']))
    grand_decay = np.mean(np.array(grand_response[type[0]]['decay']))
    labels = (['Vm', 'V'], ['t', 's'], ['t', 's'], ['t', 's'])
    feature_plot = summary_plot_pulse(feature_list,(grand_amp, grand_latency, grand_rise, grand_decay), labels,
                                  ('Amplitude', 'Latency', 'Rise time', 'Decay Tau'), c, grand_trace, plot=feature_plot,
                                  color=color, name=connection_types[c])
    if c == len(connection_types) - 1:
        x_scale = pg.ScaleBar(size=10e-3, suffix='s')
        x_scale.setParentItem(synapse_plot[c, 0].vb)
        x_scale.anchor(scale_anchor, scale_anchor, offset=scale_offset)
amp_list = feature_anova('amp', grand_response)
latency_list = feature_anova('latency', grand_response)
rise_list = feature_anova('rise', grand_response)
decay_list = feature_anova('decay', grand_response)