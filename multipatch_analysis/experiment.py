# *-* coding: utf-8 *-*
from __future__ import print_function, division
import datetime
import glob
import json
import numpy as np
import os
import sys
import re
import traceback
import pickle
from collections import OrderedDict

import yaml
import pyqtgraph as pg
import pyqtgraph.configfile

from .lims import specimen_info, specimen_images
from .constants import ALL_CRE_TYPES, ALL_LABELS, FLUOROPHORES, LAYERS
from .cell import Cell
from .electrode import Electrode
from .data import MultiPatchExperiment
from .pipette_metadata import PipetteMetadata
from .genotypes import Genotype
from .synphys_cache import SynPhysCache
from . import yaml_local, config


class Experiment(object):
    def __init__(self, entry=None, yml_file=None):
        self.entry = entry
        self.source_id = (None, None)
        self.electrodes = None
        self._cells = None
        self._connections = None
        self._gaps = None
        self._region = None
        self._summary = None
        self._view = None
        self._site_info = None
        self._slice_info = None
        self._expt_info = None
        self._lims_record = None
        self._site_path = None
        self._probed = None
        self._sweep_summary = None
        self._mosaic_file = None
        self._nwb_file = None
        self._data = None
        self._stim_list = None
        self._genotype = None
        self._cre_types = None
        self._labels = None
        self._target_layers = None
        self._rig_name = None
        
        if entry is not None:
            self._load_old_format(entry)
        else:
            self._load_yml(yml_file)

        # make sure all cells have information for all labels
        for cell in self.cells.values():
            for label in self.labels:
                if label not in cell.labels:
                    raise Exception("Cell %s is missing label %s" % (cell, label))
            for cre in self.cre_types:
                for crepart in cre.split(','):
                    if crepart != 'unknown' and crepart not in cell.labels:
                        raise Exception('Cre type "%s" not in cell.labels: %s' % (crepart, cell.labels.keys()))

        # pull donor/specimen info from LIMS
        if self.lims_record['organism'] == 'mouse':
            # lots of human donors are missing age.
            self.age
        
        if len(self.cells) > 0:
            # check for a single NWB file
            self.nwb_file

            # read cell positions from mosaic files
            try:
                self.load_cell_positions()
            except Exception as exc:
                #sys.excepthook(*sys.exc_info())
                print("Warning: Could not load cell positions for %s" % (self,))


    @staticmethod
    def _id_from_entry(entry):
        return (entry.file, entry.lines[0])

    @property
    def uid(self):
        """Return a unique ID string for this experiment.
        
        This returns the site timestamp formatted to 2 decimal places, which is
        very likely to be unique for any site.
        """
        return '%0.2f' % (self.site_info['__timestamp__'])
    
    @property
    def datetime(self):
        return datetime.datetime.fromtimestamp(self.site_info['__timestamp__'])

    @property
    def date(self):
        return self.datetime.date()

    @property
    def connections(self):
        """A list of synaptic connections reported for this experiment, excluding any that did not pass QC.
        
        Each item in the list is a tuple containing the pre- and postsynaptic cell IDs::
        
            [(pre_cell_id, post_cell_id), ...]
        """
        calls = self.connection_calls
        if calls is None:
            return None
        probed = self.connections_probed
        return [c for c in calls if c in probed]

    @property
    def connection_calls(self):
        """Manually curated list of synaptic connections seen in this experiment, without applying any QC.
        """
        return None if self._connections is None else self._connections[:]

    @property
    def gaps(self):
        """A list of electrical connections reported for this experiment, excluding any that did not pass QC.
        
        Each item in the list is a tuple containing the pre- and postsynaptic cell IDs::
        
            [(pre_cell_id, post_cell_id), ...]
        """
        calls = self.gap_calls
        if calls is None:
            return None
        probed = self.connections_probed
        return [c for c in calls if c in probed]

    @property
    def gap_calls(self):
        """Manually curated list of electrical connections seen in this experiment, without applying any QC.
        """
        return None if self._gaps is None else self._gaps[:]

    @property
    def cre_types(self):
        """A list of all cre types in this experiment."""
        if self._cre_types is None:
            cre_types = set()
            for cell in self.cells.values():
                cre_types.add(cell.cre_type)
            self._cre_types = sorted(list(cre_types), key=lambda x: ALL_CRE_TYPES.index(x.split(',')[0]))
        return self._cre_types

    @property
    def target_layers(self):
        """A list of all target layers in this experiment, similar to cre_types above"""
        if self._target_layers is None:
            target_layers = set()
            for cell in self.cells.values():
                target_layers.add(cell.target_layer)
            self._target_layers = list(target_layers)
        return self._target_layers

    @property
    def labels(self):
        """A list of all fluorophores and other markers used in this experiment."""
        if self._labels is None:
            labels = set()
            for cell in self.cells.values():
                labels |= set(cell.labels.keys()) & set(ALL_LABELS)
            self._labels = sorted(list(labels), key=lambda x: ALL_LABELS.index(x))
        return self._labels
    
    @property
    def sweep_summary(self):
        """A structure providing basic metadata on all sweeps collected in this
        experiment::
        
            [{dev_1: [stim_name, clamp_mode, holding_current, holding_potential], ...}, ...]
        """
        if self._sweep_summary is None:
            sweeps = []
            with self.data as nwb:
                for srec in nwb.contents:
                    sweep = {}
                    for dev in srec.devices:
                        rec = srec[dev]
                        sweep[dev] = rec.meta['stim_name'], rec.clamp_mode, rec.holding_current, rec.holding_potential
                    sweeps.append(sweep)
            self._sweep_summary = sweeps
        return self._sweep_summary

    def list_stims(self):
        """Open NWB file and return a list of stim set names.
        """
        if self._stim_list is None:
            stims = []
            for sweep in self.sweep_summary:
                for dev,info in sweep.items():
                    stim = info[0]
                    if stim not in stims:
                        stims.append(stim)

            # Shorten stim names
            stims = [self._short_stim_name(stim) for stim in stims]

            # sort by frequency
            def freq(stim):
                m = re.match('(.*)(\d+)Hz', stim)
                if m is None:
                    return (0, 0)
                else:
                    return (len(m.groups()[0]), int(m.groups()[1]))
            stims.sort(key=freq)

            self._stim_list = stims

        return self._stim_list

    @staticmethod
    def _short_stim_name(stim):
        if stim.startswith('PulseTrain_'):
            stim = stim[11:]
        elif stim.startswith('SPulseTrain_'):
            stim = 'S' + stim[12:]
        if stim.endswith('_DA_0'):
            stim = stim[:-5]
        if stim.endswith('H'):
            stim += 'z'
        return stim

    @property
    def cells(self):
        if self._cells is None:
            if self.electrodes is None:
                return None
            self._cells = {e.cell.cell_id:e.cell for e in self.electrodes.values() if e.cell is not None}
        return self._cells

    def _load_yml(self, yml_file):
        self.source_id = (yml_file, None)
        self._site_path = os.path.dirname(yml_file)
        self.electrodes = OrderedDict()
        
        pips = PipetteMetadata(os.path.dirname(yml_file))
        self._pipettes_yml = pips
        all_colors = set(FLUOROPHORES.values())
        genotype = self.genotype
        for pip_id, pip_meta in pips.pipettes.items():
            elec = Electrode(pip_id, pip_meta['patch_start'], pip_meta['patch_stop'], pip_meta['ad_channel'])
            self.electrodes[pip_id] = elec

            if pip_meta['got_data'] is False:
                continue

            cell = Cell(self, pip_id)
            elec.cell = cell

            cell._target_layer = pip_meta.get('target_layer', '')
            if not isinstance(cell._target_layer, str):
                raise Exception('Target layer must be str, not "%r"' % cell._target_layer)

            # load labels
            colors = {}
            for label,value in pip_meta['cell_labels'].items():
                assert label not in cell.labels
                if value == '':
                    continue
                m = re.match('(x)?(\+|\-)?(\?)?', value)
                if m is None:
                    raise Exception('Invalid label record for "%s": %s' % (label, value))

                grps = m.groups()
                absent = grps[0] == 'x'
                positive = grps[1] == '+'
                uncertain = grps[2] == '?'

                if label in ALL_LABELS:
                    cell.labels[label] = positive
                elif label in all_colors:
                    colors[label] = None if (absent or uncertain) else positive
                else:
                    raise Exception("Invalid label or fluorescent color: %s" % label)

            # check for internal dye fill
            dye = pip_meta['internal_dye']
            dye_color = FLUOROPHORES[dye]
            cell.labels[dye] = colors.get(dye_color, None)

            # decide whether each driver was expressed
            if self.lims_record['organism'] == 'mouse':
                if genotype is None:
                    raise Exception("Mouse specimen has no genotype: %s\n  (from %r)" % (self.specimen_id, self))
                for driver,positive in genotype.predict_driver_expression(colors).items():
                    cell.labels[driver] = positive

            # load QC keys
            # (sets attributes: holding_qc, access_qc, spiking_qc)
            if 'cell_qc' in pip_meta:
                for k in ['holding', 'access', 'spiking']:
                    qc_pass = pip_meta['cell_qc'][k]
                    if qc_pass == '':
                        qc_pass = None
                    else:
                        if qc_pass not in '+/-?':
                            raise ValueError('Invalid cell %s QC string: "%s"' % (k, qc_pass))
                        qc_pass = qc_pass in '+/'
                    setattr(cell, k+'_qc', qc_pass)
            else:
                # derive from NWB
                cell.holding_qc, cell.access_qc, cell.spiking_qc = self._generate_cell_qc(pip_meta['ad_channel'])
                
        # load synapse/gap connections
        for cell in self.cells.values():
            pip_meta = pips.pipettes[cell.cell_id]

            for src, dst in [('synapse_to', '_connections'), ('gap_to', '_gaps')]:
                conns = pip_meta.get(src, None)
                if conns is None:
                    continue
                conn_list = getattr(self, dst)
                if conn_list is None:
                    conn_list = []
                    setattr(self, dst, conn_list)

                for post_id in conns:
                    # allow tentative connections like "4?"
                    if isinstance(post_id, str):
                        m = re.match("^(\d+)(\?)?$", post_id)
                        if m is None:
                            post_id = None  # triggers ValueError below
                        post_id = int(m.groups()[0])
                        if m.groups()[1] == '?':
                            # ignore questionable connections for now
                            continue
                    if post_id not in self.cells:
                        raise ValueError("Postsynaptic cell ID %r is invalid" % post_id)
                    conn_list.append((cell.cell_id, post_id))
                
    def _generate_cell_qc(self, ad_chan):
        # tempporary qc used to decide how many connections were probed in an
        # experiment. will be replaced with per-pulse-response qc later.
        cache_file = os.path.join(os.path.dirname(config.configfile), 'cell_qc_cache.pkl')
        
        cache = {}
        if os.path.isfile(cache_file):
            try:
                cache = pickle.load(open(cache_file, 'rb'))
            except Exception:
                sys.excepthook(*sys.exc_info())
                print("Failed to load cell qc cache (error above).")
        
        cache_key = (self.nwb_file, ad_chan)
        if cache_key not in cache:
            nwb = self.data
            holding_qc = False
            access_qc = False
            spiking_qc = False
            try:
                passed_holding = 0
                for srec in nwb.contents:
                    try:
                        rec = srec[ad_chan]
                    except KeyError:
                        continue
                    if rec.clamp_mode == 'vc':
                        if rec.baseline_current is not None and abs(rec.baseline_current) < 800e-12:
                            passed_holding += 1
                    else:
                        vm = rec.baseline_potential
                        if vm > -75e-3 and vm < -50e-3:
                            passed_holding += 1
                    if passed_holding >= 5:
                        break
                if passed_holding >= 5:
                    holding_qc = True
                    # need to fix these!
                    access_qc = True
                    spiking_qc = True
            finally:
                self.close_data()
            cache[cache_key] = (holding_qc, access_qc, spiking_qc)
            
            tmp_file = cache_file+'_tmp'
            pickle.dump(cache, open(tmp_file, 'wb'))
            if os.path.exists(cache_file):
                os.remove(cache_file)
            os.rename(tmp_file, cache_file)
            
        return cache[cache_key]

    def _load_old_format(self, entry):
        """Load experiment metadata from an old-style summary file
        """
        try:
            self.source_id = self._id_from_entry(entry)
        except Exception as exc:
            Exception("Error parsing experiment: %s\n%s" % (self, exc.args[0]))

        self.electrodes = OrderedDict()
        for i in range(1,9):
            # Ideally, this is the only place we would bake in this assumption:
            ad_channel = i - 1

            elec = Electrode(i, None, None, ad_channel)
            self.electrodes[i] = elec
            elec.cell = Cell(self, i)
    
        have_connections = False
        have_labels = False
        for ch in entry.children:
            try:
                if ch.lines[0] == 'Labeling':
                    self._parse_labeling(ch)
                    have_labels = True
                elif ch.lines[0] == 'Cell QC':
                    self._parse_qc(ch)
                elif ch.lines[0] == 'Connections':
                    self._connections = []
                    self._parse_connections(ch)
                    have_connections = True
                elif ch.lines[0] == 'Conditions':
                    continue
                elif ch.lines[0].startswith('Region '):
                    assert len(ch.children) == 0
                    self._region = ch.lines[0][7:]
                elif ch.lines[0].startswith('Site path '):
                    if len(ch.children) > 0:
                        raise Exception("Site path entry should not have children in experiment %s" % self)
                    p = os.path.abspath(os.path.join(os.path.dirname(self.entry.file), ch.lines[0][10:]))
                    if not os.path.isdir(p):
                        raise Exception("Invalid site path: %s" % p)
                    self._site_path = p
                else:
                    raise Exception('Invalid experiment entry "%s"' % ch.lines[0])

            except Exception as exc:
                traceback.print_exc()
                raise Exception("Error parsing %s for experiment: %s\n%s" % (ch.lines[0], self, exc.args))

        if have_labels is False:
            raise Exception("Experiment %s is missing Labeling section" % self)
        if have_connections is False:
            raise Exception("Experiment %s is missing Connections section" % self)

    def _parse_labeling(self, entry):
        """
        "Labeling" section should look like:
        
            Labeling:
                sim1: 1+ 2- 3x 4x+ 5+? 6?
                biocytin: ...
                af488: 1+ 2+ 3x 4- 5? 6+
                cascade_blue: ...
                
        This example has the following interpretation:
        
            1+   Cell 1 is reporter-positive and dye filled
            2-   Cell 2 is reporter-negative and dye filled   
            3x   Cell 3 type cannot be determined (no pipette tip found)
            4x+  Cell 4 was not dye filled, but pipette tip appears to be touching cre-positive cell
            5+?  Cell 5 looks like cre-positive, but image is somewhat unclear
            6?   Cell 6 is dye filled, but cre type is ambiguous
        """
        for ch in entry.children:
            line = ch.lines[0]
            # line looks like "sim1: 1+ 2-'

            parts = re.split('\s+', line)

            # first part is label / cre type and a colon
            assert parts[0].endswith(':')
            cre = parts[0][:-1]

            # old labels used to mark target layer
            layer_labels = ['L1', 'L23pyr', 'L4pyr', 'L5pyr', 'L6pyr']
            if not (cre in ALL_LABELS or cre in ALL_CRE_TYPES or cre.lower().startswith('human_l') or cre in layer_labels):
                raise Exception("Invalid label or cre type: %s" % cre)

            # parse the remainder of the line
            if len(parts[1:]) == 1 and parts[1].strip() == '?':
                # no data
                continue

            for part in parts[1:]:
                m = re.match('(\d+)(x)?(\+|\-)?(\?)?', part)
                if m is None:
                    raise Exception('invalid label record: %s' % part)
                grps = m.groups()
                cell_id = int(grps[0])
                cell = self.cells[cell_id]
                absent = grps[1] == 'x'
                positive = grps[2]
                uncertain = grps[3] == '?'
                cell._raw_labels[cre] = ''.join([x or '' for x in grps[1:]])

                # some target layers have been entered as a label (old data)
                if cre.startswith('human_') and positive == '+':
                    # positive=='+' is actually currently used to mean that the cell is in this layer and excitatory,
                    # but for now we are just recording the layer and excluding all other cells.
                    layer = cre[7:].upper()
                    if layer == '23':
                        layer = '2/3'
                    cell._target_layer = layer
                elif cre in layer_labels:
                    # labels like "L23pyr" were used to denote unlabeled cells that are likely pyramidal,
                    # but where the morphology may not have been verified.
                    layer = cre.lstrip('L').rstrip('pyr')
                    if layer == '23':
                        layer = '2/3'
                    cell._target_layer = layer
                else:
                    assert cre not in cell.labels
                    cell.labels[cre] = positive

    def _parse_qc(self, entry):
        """Parse cell quality control. Looks like:
        
            Holding: 1- 2+ 3- 4- 6/ 7+
            Access: 1- 2/ 3+ 4- 6/ 7/
            Spiking: 1- 2+ 3+ 4- 6+ 7+
        
        Where + means pass, / means borderline pass, - means fail, ? means unknown
        """
        for ch in entry.children:
            parts = re.split('\s+', ch.lines[0].strip())
            for part in parts[1:]:
                m = re.match(r'(\d+)(\+|\/|\-|\?)', part)
                if m is None:
                    raise Exception('Invalid cell QC string "%s"' % part)
                cell_id = int(m.groups()[0])
                val = m.groups()[1]
                cell = self.cells[cell_id]

                if parts[0] == 'Holding:':
                    cell.holding_qc = val in '+/'
                elif parts[0] == 'Access:':
                    cell.access_qc = val in '+/'
                elif parts[0] == 'Spiking:':
                    cell.spiking_qc = val in '+/'
                else:
                    raise Exception("Invalid Cell QC line: %s" % ch.lines[0])

    def _parse_connections(self, entry):
        if len(entry.children) == 0 or entry.children[0].lines[0] == 'None':
            return
        for con in entry.children:
            m = re.match(r'(\d+)\s*->\s*(\d+)\s*(\??)\s*(.*)', con.lines[0].strip())
            if m is None:
                raise Exception("Invalid connection: %s" % con.lines[0])

            if m.groups()[2] == '?':
                # ignore questionable connections
                continue
            self._connections.append((int(m.groups()[0]), int(m.groups()[1])))

    def summary(self):
        """Return a structure summarizing (non)connectivity in the experiment.
        
        Looks like:
        
            {(pre_type, post_type): {
                'connected': n, 
                'unconnected': m, 
                'cdist': [...], 
                'udist': [...]
                }, 
            ...}
        """
        if self.connections is None:
            return None
        if self._summary is None:
            csum = {}
            for i, j in self.connections_probed:
                ci, cj = self.cells[i], self.cells[j]
                typ = ((ci.target_layer, ci.cre_type), (cj.target_layer, cj.cre_type))
                if typ not in csum:
                    csum[typ] = {'connected': 0, 'unconnected': 0, 'cdist':[], 'udist':[]}
                if (i, j) in self.connections:
                    csum[typ]['connected'] += 1
                    csum[typ]['cdist'].append(ci.distance(cj))
                else:
                    csum[typ]['unconnected'] += 1
                    csum[typ]['udist'].append(ci.distance(cj))
            self._summary = csum
        return self._summary

    @property
    def region(self):
        return 'V1' if (not hasattr(self, '_region') or self._region is None) else self._region

    @property
    def connections_probed(self):
        """A list of probed connections (pre_cell, post_cell) that passed QC.
        """
        if self._probed is None:
            probed = []
            for i,ci in self.cells.items():
                for j,cj in self.cells.items():
                    if i == j:
                        continue
                    if ci.spiking_qc is not True:
                        # presynaptic cell failed spike QC; ignore
                        continue
                    if cj.pass_qc is not True:
                        # postsynaptic cell failed ephys qc; ignore
                        continue
                    if ci.cre_type is None or cj.cre_type is None:
                        # indeterminate cell types; ignore
                        #print("Ignore probe (ind. cell type) %s:%d-%d" % (self.source_id, i, j))
                        #if (i, j) in self.connections:
                            #print("    --> connected!")
                        continue
                    probed.append((i, j))
            self._probed = probed
        return self._probed

    @property
    def n_connections_probed(self):
        return len(self.connections_probed)

    @property
    def n_connections(self):
        summary = self.summary()
        if summary is None:
            return None
        return sum([x['connected'] for x in summary.values()])

    def load_cell_positions(self):
        """Load cell positions from external file.
        """
        sitefile = self.mosaic_file
        mosaic = json.load(open(sitefile))
        marker_items = [i for i in mosaic['items'] if i['type'] == 'MarkersCanvasItem']
        if len(marker_items) == 0:
            raise TypeError("No cell markers found in site mosaic file %s" % sitefile)
        elif len(marker_items) > 1:
            raise TypeError("Multiple marker items found in site mosaic file %s" % sitefile)
        cells = marker_items[0]['markers']
        for name, pos in cells:
            m = re.match("\D+(\d+)", name)
            cid = int(m.group(1))
            if cid in self.cells:
                self.cells[cid].position = pos

    @property
    def mosaic_file(self):
        """Path to site mosaic file
        """
        if self._mosaic_file is None:
            sitefile = os.path.join(self.path, "site.mosaic")
            if not os.path.isfile(sitefile):
                sitefile = os.path.join(os.path.split(self.path)[0], "site.mosaic")
            if not os.path.isfile(sitefile):
                mosaicfiles = [f for f in os.listdir(self.path) if f.endswith('.mosaic')]
                if len(mosaicfiles) == 1:
                    sitefile = os.path.join(self.path, mosaicfiles[0])
            if not os.path.isfile(sitefile):
                # print(os.listdir(self.path))
                # print(os.listdir(os.path.split(self.path)[0]))
                raise Exception("No site mosaic found for %s" % self)
            self._mosaic_file = sitefile
        return self._mosaic_file

    @property
    def path(self):
        """Filesystem path to the root of this experiment.
        """
        if self._site_path is None:
            date, slice, site = self.source_id[1].split('-')
            root = os.path.dirname(self.source_id[0])
            if '_' not in date:
                date += '_000'
            paths = [
                os.path.join(root, date, "slice_%03d"%int(slice), "site_%03d"%int(site)),
                os.path.join(root, 'V1', date, "slice_%03d"%int(slice), "site_%03d"%int(site)),
                os.path.join(root, 'ALM', date, "slice_%03d"%int(slice), "site_%03d"%int(site)),
                os.path.join(root, 'Human', date, "slice_%03d"%int(slice), "site_%03d"%int(site)),
                # missing data, still in versioned backups
                os.path.join(root, '..', '..', '..', 'version_backups', 'data', 'Alex', 'V1', date, "slice_%03d" % int(slice), "site_%03d" % int(site)),
            ]
            for path in paths:
                if os.path.isdir(path):
                    self._site_path = path
                    break
            if self._site_path is None:
                raise Exception("Cannot find filesystem path for experiment %s. Attempted paths:\n%s" % (self, "\n".join(paths)))
        return self._site_path

    def __repr__(self):
        try:
            uid = self.uid
        except Exception as exc:
            uid = '?'
        
        if self.entry is None:
            src = self.source_id[0]
        else:
            # old format
            src = "%s (%s:%d)" % (self.source_id[1], self.source_id[0], self.entry.lineno)

        return "<Experiment %s uid=%s>" % (src, uid)

    @property
    def site_info(self):
        if self._site_info is None:
            index = os.path.join(self.path, '.index')
            if not os.path.isfile(index):
                raise TypeError("Cannot find slice index file (%s) for experiment %s" % (index, self))
            self._site_info = pg.configfile.readConfigFile(index)['.']
        return self._site_info

    @property
    def slice_info(self):
        if self._slice_info is None:
            index = os.path.join(os.path.split(self.path)[0], '.index')
            if not os.path.isfile(index):
                raise TypeError("Cannot find slice index file (%s) for experiment %s" % (index, self))
            self._slice_info = pg.configfile.readConfigFile(index)['.']
        return self._slice_info

    @property
    def slice_timestamp(self):
        return datetime.datetime.fromtimestamp(self.slice_info['__timestamp__'])

    @property
    def slice_dir(self):
        return os.path.join(self.path, '..')

    @property
    def expt_info(self):
        if self._expt_info is None:
            index = os.path.join(self.path, '..', '..', '.index')
            if not os.path.isfile(index):
                raise TypeError("Cannot find index file (%s) for experiment %s" % (index, self))
            self._expt_info = pg.configfile.readConfigFile(index)['.']
        return self._expt_info

    @property
    def nwb_file(self):
        if self._nwb_file is None:
            p = self.path
            files = glob.glob(os.path.join(p, '*.nwb'))
            if len(files) == 0:
                files = glob.glob(os.path.join(p, '*.NWB'))
            if len(files) == 0:
                raise Exception("No NWB file found for %s\nSearched in path %s" % (self, self.path))
            elif len(files) > 1:
                # multiple NWB files here; try using the file manifest to resolve.
                manifest = os.path.join(self.path, 'file_manifest.yml')
                if os.path.isfile(manifest):
                    manifest = yaml.load(open(manifest, 'rb'))
                    for f in manifest:
                        if f['category'] == 'MIES physiology':
                            self._nwb_file = os.path.join(os.path.dirname(self.path), f['path'])
                            break
                if self._nwb_file is None:
                    raise Exception("Multiple NWB files found for %s" % self)
            self._nwb_file = files[0]
        return self._nwb_file

    @property
    def nwb_cache_file(self):
        try:
            return SynPhysCache().get_cache(self.nwb_file)
        except:
            # deprecated soon..
            if not os.path.isdir('cache'):
                os.mkdir('cache')
            cf = os.path.join('cache', self.nwb_file.replace('/', '_').replace(':', '_').replace('\\', '_'))
            if not os.path.isfile(cf) or os.stat(self.nwb_file).st_mtime > os.stat(cf).st_mtime:
                try:
                    import shutil
                    print("copying to cache:", cf)
                    shutil.copyfile(self.nwb_file, cf)
                except:
                    if os.path.isfile(cf):
                        os.remove(cf)
                    raise
            return cf

    @property
    def data(self):
        """Data object from NWB file. 
        
        Contains all ephys recordings.
        """
        if self._data is None:
            try:
                self._data = MultiPatchExperiment(self.nwb_cache_file)
            except IOError:
                os.remove(self.nwb_cache_file)
                self._data = MultiPatchExperiment(self.nwb_cache_file)
        return self._data

    def close_data(self):
        self.data.close()
        self._data = None

    @property
    def specimen_id(self):
        return self.slice_info['specimen_ID'].strip()

    @property
    def age(self):
        age = self.lims_record.get('age', 0)
        if self.lims_record['organism'] == 'mouse':
            if age == 0:
                raise Exception("Donor age not set in LIMS for specimen %s" % self.specimen_id)
            # data not entered in to lims
            age = (self.date - self.birth_date).days
        else:
            age = np.nan
        return age

    @property
    def birth_date(self):
        bd = self.lims_record['date_of_birth']
        return datetime.date(bd.year, bd.month, bd.day)

    @property
    def lims_record(self):
        """A dictionary of specimen information queried from LIMS.
        
        See multipatch_analysis.lims.section_info()
        """
        if self._lims_record is None:
            self._lims_record = specimen_info(self.specimen_id)
        return self._lims_record

    @property
    def genotype(self):
        """The genotype of this specimen.
        """
        if self._genotype is None:
            gt_name = self.lims_record['genotype']
            if gt_name is None:
                return None
            self._genotype = Genotype(gt_name)
        return self._genotype

    @property
    def biocytin_image_url(self):
        """A LIMS URL that points to the biocytin image for this specimen, or
        None if no image is found.
        """
        images = specimen_images(self.specimen_id)
        for img_id, treatment in images:
            if treatment == 'Biocytin':
                return "http://lims2/siv?sub_image=%d" % img_id

    @property
    def dapi_image_url(self):
        """A LIMS URL that points to the DAPI image for this specimen, or
        None if no image is found.
        """
        images = specimen_images(self.specimen_id)
        for img_id, treatment in images:
            if treatment == 'DAPI':
                return "http://lims2/siv?sub_image=%d" % img_id

    @property
    def multipatch_log(self):
        files = [p for p in os.listdir(self.path) if re.match(r'MultiPatch_\d+.log', p)]
        if len(files) == 0:
            raise TypeError("Could not find multipatch log file for %s" % self)
        if len(files) > 1:
            raise TypeError("Found multiple multipatch log files for %s" % self)
        return os.path.join(self.path, files[0])

    @property
    def surface_depth(self):
        try:
            mplog = self.multipatch_log
        except TypeError:
            return None
        lines = [l for l in open(mplog, 'rb').readlines() if 'surface_depth_changed' in l]
        if len(lines) == 0:
            return None
        line = lines[-1].rstrip(',\r\n')
        return json.loads(line)['surface_depth']

    @property
    def target_temperature(self):
        """The intended temperature of the experiment in °C, or None.

        If the temperature was recorded as "RT", then 22.0 is returned.
        """
        temp = self.expt_info.get('temperature')
        if temp is not None:
            temp = temp.lower().rstrip(' c').strip()
            if temp == 'rt':
                temp = 22.0
            elif temp == '':
                temp = None
            else:
                try:
                    temp = float(temp)
                except Exception:
                    raise ValueError('Invalid temperature: "%s"' % self.expt_info.get('temperature'))
        return temp

    @property
    def original_path(self):
        """The original path where this experiment was acquired. 
        """
        ss = os.path.join(self.path, 'sync_source')
        if os.path.isfile(ss):
            return open(ss, 'rb').read()
        else:
            return self.path

    @property
    def relative_path(self):
        """The path of this experiment relative to the data repository it lives in.
        """
        repo_path = os.path.abspath(os.path.join(self.path, '..', '..', '..'))
        return os.path.relpath(self.path, repo_path)

    @property
    def server_path(self):
        """The path of this experiment relative to the server storage directory.
        """
        expt_timestamp = self.expt_info['__timestamp__']
        path_file = os.path.join(config.synphys_data, 'experiment_path_cache.pkl')
        paths = pickle.load(open(path_file, 'rb'))
        expt_path = paths.get(expt_timestamp, None)
        if expt_path is None:
            return None
        rel = self.path.split(os.path.sep)[-2:]
        return os.path.join(expt_path, *rel)

    @property
    def rig_name(self):
        """The name of the rig used to acquire this experiment.
        """
        if self._rig_name is None:
            self._rig_name = self.expt_info.get('rig_name', None)
            if self._rig_name is None:
                path = self.original_path
                m = re.search(r'\/(MP\d)', self.original_path)
                if m is not None:
                   self._rig_name = m.groups()[0]
        return self._rig_name

    def show(self):
        if self._view is None:
            pg.mkQApp()
            self._view_widget = pg.GraphicsLayoutWidget()
            self._view = self._view_widget.addViewBox(0, 0)
            v = self._view
            cell_ids = sorted(self.cells.keys())
            pos = np.array([self.cells[i].position[:2] for i in cell_ids])
            if len(self.connections) == 0:
                adj = np.empty((0,2), dtype='int')
            else:
                adj = np.array(self.connections) - 1
            colors = []
            for cid in cell_ids:
                cell = self.cells[cid]
                color = [0, 0, 0]
                for i,cre in enumerate(self.cre_types):
                    if cell.labels[cre] == '+':
                        color[i] = 255
                colors.append(color)
            brushes = [pg.mkBrush(c) for c in colors]
            print(pos)
            print(adj)
            print(colors)
            self._graph = pg.GraphItem(pos=pos, adj=adj, size=30, symbolBrush=brushes)
            v.addItem(self._graph)
        self._view_widget.show()