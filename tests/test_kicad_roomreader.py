"""Opt-in real-design acceptance; no design bytes or geometry live in this repo.

Run on Friedl's Mac with KiCad installed. ALX_ROOMREADER_BOARD and
ALX_ROOMREADER_ZIP override the read-only Desktop inputs. All derived geometry,
scripts, work copies and exports have only pytest's temporary lifetime.
"""
from collections import Counter
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SOURCE))

from alx.bootstrap.kicad import build_kicad_runtime
from alx.config.settings import KiCadSettings
from alx.contracts import CapabilityResultState
from alx.providers.kicad import Node, sexpr


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _gerber_geometry(text, *, linear_regions=True):
    """Read KiCad's metric, absolute linear regions and stroke endpoints.

    This is deliberately not the equality oracle: the public export comparison
    still checks every manufacturing record, including apertures and ordering.
    Fail on unsupported window geometry rather than guessing a bounding box.
    Coordinates remain exact decimal mm until pcbnew's integer conversion.
    """
    fmt = re.search(r'%FSLAX\d(\d)Y\d(\d)\*%', text)
    assert fmt and fmt[1] == fmt[2] and '%MOMM*%' in text
    scale = Decimal(10) ** -int(fmt[1])
    text = re.sub(r'G04[^*]*\*', '', text)
    text = re.sub(r'%.*?%', '', text, flags=re.S)
    point = (Decimal(0), Decimal(0))
    mode = None
    interpolation = 'G01'
    region = None
    regions, strokes = [], set()
    for command in text.split('*'):
        command = command.strip()
        if not command:
            continue
        if command == 'G36':
            region = []
            continue
        if command == 'G37':
            if linear_regions:
                assert region and region[0] == region[-1], 'Unclosed coverlay region'
                regions.append(tuple(region))
            region = None
            continue
        interp = re.match(r'G0[123]', command)
        if interp:
            interpolation = interp[0]
            command = command[len(interp[0]):]
        operation = re.search(r'D0([123])$', command)
        if operation:
            mode = operation[1]
        coords = dict(re.findall(r'([XY])([+-]?\d+)', command))
        if not coords:
            continue
        next_point = (Decimal(coords['X']) * scale if 'X' in coords else point[0],
                      Decimal(coords['Y']) * scale if 'Y' in coords else point[1])
        if region is not None:
            if linear_regions:
                assert interpolation == 'G01', 'Curved coverlay region needs explicit support'
            region.append(next_point)
        elif mode == '1' and interpolation == 'G01':
            strokes.add(tuple(sorted((point, next_point))))
        point = next_point
    return regions, strokes


def _function(text):
    match = re.search(r'(?:%TF|; #@! TF)\.FileFunction,([^*\r\n]+)', text)
    return match[1] if match else None


def _manufacturing_kind(data, name):
    text = data.decode('utf-8', errors='replace')
    if Path(name).suffix.lower() == '.drl' or re.search(r'(?m)^\s*M48\s*$', text):
        return 'drill'
    if re.search(r'%FS|%TF\.|(?m:^\s*G04)', text):
        return 'gerber'
    return 'other'


def _archive_diagnostics(previous):
    return 'zip members / FileFunctions: ' + repr([
        (name, _function(previous[name].decode(errors='replace')))
        for name in sorted(previous)])


def _reference_mapping(previous, baseline_layers):
    protel = {'.gtl': 'F.Cu', '.gbl': 'B.Cu', '.gts': 'F.Mask', '.gbs': 'B.Mask',
              '.gto': 'F.SilkS', '.gbo': 'B.SilkS', '.gtp': 'F.Paste',
              '.gbp': 'B.Paste', '.gm1': 'Edge.Cuts', '.gko': 'Edge.Cuts'}
    diagnostics = _archive_diagnostics(previous)
    reference_layers, renames = {}, {}
    for name, data in previous.items():
        if _manufacturing_kind(data, name) != 'gerber':
            continue
        text = data.decode()
        if Path(name).name == 'pib_0.15.gbr':
            matches = ['User.1']
        else:
            function = _function(text)
            matches = [layer for layer, old in baseline_layers.items()
                       if function and _function(old) == function]
            # User functions can be shared; their explicit names disambiguate.
            if len(matches) > 1:
                matches = [layer for layer in matches
                           if Path(name).stem.endswith('-' + layer.replace('.', '_'))
                           or layer in function.split(',')]
            if not matches:
                layer = protel.get(Path(name).suffix.lower())
                matches = [layer] if layer in baseline_layers else []
            if not matches:
                matches = [layer for layer in baseline_layers
                           if Path(name).stem.endswith('-' + layer.replace('.', '_'))]
        assert len(matches) == 1, f'Ambiguous reference layer: {name}; {diagnostics}'
        layer = matches[0]
        assert layer not in reference_layers, f'Duplicate reference layer: {name}; {diagnostics}'
        reference_layers[layer] = text
        renames[layer] = name
    assert renames.get('User.1') == 'pib_0.15.gbr', diagnostics
    assert 'F.Mask' in reference_layers, diagnostics
    return reference_layers, renames


def _drill_reference_name(previous, data):
    function = _function(data.decode(errors='replace'))
    assert function and function.split(',')[0] in ('Plated', 'NonPlated'), (
        'Expected separate Excellon files with plated/non-plated X2 FileFunction')
    matches = [name for name, content in previous.items()
               if _manufacturing_kind(content, name) == 'drill'
               and _function(content.decode(errors='replace')) == function]
    assert len(matches) == 1, 'Ambiguous drill FileFunction; ' + _archive_diagnostics(previous)
    return matches[0]


def test_drills_match_by_x2_without_merging_or_ignoring_geometry():
    from alx.providers.kicad import normalized_manufacturing
    plated = b'; #@! TF.FileFunction,Plated,1,2,PTH\nM48\nT1C0.25\nX1Y2\nM30\n'
    nonplated = plated.replace(b'Plated,1,2,PTH', b'NonPlated,1,2,NPTH')
    previous = {'synthetic-PTH.drl': plated, 'synthetic-NPTH.drl': nonplated}
    assert _drill_reference_name(previous, plated) == 'synthetic-PTH.drl'
    assert _drill_reference_name(previous, nonplated) == 'synthetic-NPTH.drl'
    with pytest.raises(AssertionError, match='Expected separate Excellon'):
        _drill_reference_name(previous, b'M48\nT1C0.25\nM30\n')
    assert normalized_manufacturing(plated) != normalized_manufacturing(nonplated)
    assert normalized_manufacturing(plated) != normalized_manufacturing(plated.replace(b'X1Y2', b'X3Y4'))


def test_reference_mapping_accepts_protel_and_extensionless_gerbers():
    # Artificial format examples only: no external design data or geometry.
    mask = b'%FSLAX46Y46*%\n%TF.FileFunction,Soldermask,Top*%\nM02*'
    copper = b'G04 synthetic Gerber*\nM02*'
    stiffener = b'%TF.FileFunction,Other,User*%\nM02*'
    previous = {'example.gts': mask, 'example.gbl': copper,
                'pib_0.15.gbr': stiffener, 'example.drl': b'M48\nM30'}
    layers, renames = _reference_mapping(previous, {'F.Mask': mask.decode(),
                                                   'B.Cu': copper.decode()})
    assert set(layers) == {'F.Mask', 'B.Cu', 'User.1'}
    assert renames['F.Mask'] == 'example.gts'
    assert _manufacturing_kind(b'M48\nM30', 'holes.unknown') == 'drill'
    assert _manufacturing_kind(mask, 'mask.unknown') == 'gerber'
    # X2 wins over a conflicting extension, and extensionless members map too.
    previous['example.gbl'] = mask
    del previous['example.gts']
    previous['copper.unknown'] = b'%TF.FileFunction,Copper,L2,Bot*%\nM02*'
    layers, renames = _reference_mapping(previous, {
        'F.Mask': mask.decode(), 'B.Cu': previous['copper.unknown'].decode()})
    assert renames['F.Mask'] == 'example.gbl'
    assert renames['B.Cu'] == 'copper.unknown'


def test_reference_mapping_reports_archive_inventory():
    previous = {'unmapped.unknown': b'%TF.FileFunction,Other,Unknown*%\nM02*',
                'holes.drl': b'M48\nM30'}
    with pytest.raises(AssertionError) as failure:
        _reference_mapping(previous, {})
    diagnostic = str(failure.value)
    assert 'unmapped.unknown' in diagnostic
    assert 'holes.drl' in diagnostic
    assert 'Other,Unknown' in diagnostic



def _succeeded(runtime, capability, arguments):
    result = runtime.executors[capability](arguments)
    # Never include stdout, board diffs or reference geometry in assertion output.
    assert result.state == CapabilityResultState.SUCCEEDED, (capability, result.failure)
    return result.values


def test_roomreader_stiffener_matches_production(tmp_path):
    folder = Path.home() / 'Desktop/BHL_RoomReaderV2_antenna_KiCAD'
    board = Path(os.environ.get('ALX_ROOMREADER_BOARD', str(
        folder / 'BHL_RoomReaderV2 Antenna.kicad_pcb.before-stiffener'))).expanduser()
    reference = Path(os.environ.get('ALX_ROOMREADER_ZIP', str(
        folder / 'production/BHL_RoomReaderV2_Antenna_stiffener.zip'))).expanduser()
    # Strip only the backup suffix; the project must keep the board's basename.
    board_name = board.name.split('.kicad_pcb', 1)[0] + '.kicad_pcb'
    project = board.with_name(Path(board_name).with_suffix('.kicad_pro').name)
    originals = (board, project, reference)
    stamps = {}
    try:
        for path in originals:
            stamps[path] = _stamp(path)
            with path.open('rb') as stream:
                # Probe the entire file, not merely stat/access or its first byte.
                while stream.read(1024 * 1024):
                    pass
    except OSError as error:
        pytest.skip(f'RoomReader input unavailable: {path}: {error}')

    try:
        cli = os.environ.get('ALX_KICAD_CLI') or shutil.which('kicad-cli') or str(
            Path('/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli'))
        python = os.environ.get('ALX_KICAD_PYTHON', str(
            KiCadSettings.__dataclass_fields__['kicad_python'].default))
        try:
            for argv in ([cli, '--version'], [python, '-B', '-c', 'import pcbnew']):
                probe = subprocess.run(argv, capture_output=True, timeout=30, check=False)
                if probe.returncode:
                    pytest.skip('RoomReader requires working kicad-cli and KiCad pcbnew Python')
        except (OSError, subprocess.TimeoutExpired) as error:
            pytest.skip(f'RoomReader KiCad installation unavailable: {error}')

        temporary_project = tmp_path / 'designs/roomreader'
        temporary_project.mkdir(parents=True)
        try:
            # Explicit read-only opens: never give a capability an original board.
            for source, name in ((board, board_name), (project, project.name)):
                with source.open('rb') as src, (temporary_project / name).open('wb') as dst:
                    shutil.copyfileobj(src, dst)
            with reference.open('rb') as stream, zipfile.ZipFile(stream) as archive:
                previous = {info.filename: archive.read(info) for info in archive.infolist()
                            if not info.is_dir()}
        except OSError as error:
            pytest.skip(f'RoomReader input became unreadable: {error}')

        tree = sexpr((temporary_project / board_name).read_text())
        layers = {n.children[1]: n for n in tree.one('layers').children if isinstance(n, Node)}
        runtime = build_kicad_runtime(lambda: 'roomreader-acceptance', {
            'ALX_KICAD_CLI': cli, 'ALX_KICAD_PYTHON': python,
            'ALX_KICAD_DESIGN_ROOTS': str(tmp_path / 'designs'),
        }, tmp_path / 'storage')
        checked = _succeeded(runtime, 'check_kicad_board', {
            'project_dir': str(temporary_project)})
        work_id = checked['work_copy_id']
        baseline = _succeeded(runtime, 'export_kicad_manufacturing', {
            'work_copy_id': work_id, 'output_dir': 'baseline', 'separate_drills': True})
        baseline_files = {Path(p).name: Path(p).read_bytes() for p in baseline['files_written']}
        baseline_layers = {}
        for layer, node in layers.items():
            aliases = [str(value).replace('.', '_') for value in node.children[1:]
                       if value not in ('user', 'signal')]
            candidates = [name for name, data in baseline_files.items()
                          if _manufacturing_kind(data, name) == 'gerber'
                          and Path(name).stem in {Path(board_name).stem + '-' + a for a in aliases}]
            if candidates:
                assert len(candidates) == 1
                baseline_layers[layer] = baseline_files[candidates[0]].decode()

        reference_layers, renames = _reference_mapping(previous, baseline_layers)

        old_regions, old_strokes = _gerber_geometry(baseline_layers['F.Mask'])
        new_regions, new_strokes = _gerber_geometry(reference_layers['F.Mask'])
        added = list((Counter(new_regions) - Counter(old_regions)).elements())
        assert len(added) == 1, 'Reference must add exactly one top-coverlay region'
        # KiCad Gerbers invert Y, with no auxiliary-origin flag in this exporter.
        window = [(str(x), str(-y)) for x, y in added[0][:-1]]
        missing_strokes = old_strokes - new_strokes
        assert missing_strokes, 'Expected top-coverlay note strokes to be removed'
        note_layers = [layer for layer, text in reference_layers.items()
                       if layer != 'F.Mask' and
                       missing_strokes <= _gerber_geometry(text, linear_regions=False)[1]]
        assert len(note_layers) <= 1, 'Reference note destination is ambiguous'
        # Unplotted notes can live on a nonmanufacturing user layer. If they are
        # plotted, their stroke geometry identifies the destination in the zip.
        note_layer = note_layers[0] if note_layers else 'Cmts.User'

        # Preserve reference drill/job names too, classifying Excellon separately
        # even when the archive uses a nonstandard extension.
        diagnostics = _archive_diagnostics(previous)
        used = set(renames.values())
        for name, data in baseline_files.items():
            kind = _manufacturing_kind(data, name)
            if kind == 'gerber':
                continue
            matches = [old for old, content in previous.items()
                       if old not in used and _manufacturing_kind(content, old) == kind]
            if kind == 'drill':
                matches = [_drill_reference_name(previous, data)]
                assert matches[0] not in used, 'Duplicate exported drill FileFunction'
            else:
                matches = [old for old in matches if Path(old).suffix == Path(name).suffix]
            assert len(matches) == 1, f'Ambiguous manufacturing filename: {name}; {diagnostics}'
            renames[name] = matches[0]
            used.add(matches[0])
        assert used == set(previous), f'Unmapped reference members: {set(previous) - used}; {diagnostics}'

        script = '''
import json, sys
from decimal import Decimal
from pathlib import Path
import re
import pcbnew
path = Path(sys.argv[1])
board = pcbnew.LoadBoard(str(path))
stiffener = pcbnew.User_1
# Canonical file names differ from display names (Cmts.User/User.Comments).
# Resolve installed enum values: numeric layer IDs changed between KiCad versions.
notes = getattr(pcbnew, NOTE_LAYER.replace('.', '_'), None)
if notes is None:
    notes = board.GetLayerID(NOTE_LAYER)
assert notes >= 0
board.SetLayerName(stiffener, 'pib_0.15')
enabled = board.GetEnabledLayers()
enabled.AddLayer(stiffener)
enabled.AddLayer(notes)
board.SetEnabledLayers(enabled)
moved_stiffener, moved_notes = [], []
# The stiffener is a filled zone on B.Fab: redraw its outline as a filled
# shape on the stiffener layer and remove the zone.
for zone in [z for z in board.Zones() if z.GetLayer() == pcbnew.B_Fab]:
    outline = pcbnew.PCB_SHAPE()
    outline.SetShape(pcbnew.SHAPE_T_POLY)
    outline.SetLayer(stiffener)
    outline.SetFilled(True)
    outline.SetWidth(0)
    outline.SetPolyShape(zone.Outline())
    board.Add(outline)
    board.Remove(zone)
    moved_stiffener.append(str(outline.m_Uuid.AsString()))
# Notes and dimensions drawn on the top coverlay are not coverlay openings.
for item in list(board.GetDrawings()):
    if item.GetLayer() == pcbnew.F_Mask and isinstance(item, (pcbnew.PCB_TEXT, pcbnew.PCB_DIMENSION_BASE)):
        item.SetLayer(notes)
        moved_notes.append(str(item.m_Uuid.AsString()))
assert moved_stiffener and moved_notes
shape = pcbnew.PCB_SHAPE()
shape.SetShape(pcbnew.SHAPE_T_POLY)
shape.SetLayer(pcbnew.F_Mask)
shape.SetFilled(True)
shape.SetWidth(0)
polygon = pcbnew.SHAPE_POLY_SET()
polygon.NewOutline()
# WINDOW is in the production Gerber's frame: offset from the drill/aux
# origin with Y pointing up. Convert it to board coordinates.
aux = board.GetDesignSettings().GetAuxOrigin()
for x, y in WINDOW:
    polygon.Append(aux.x + int(Decimal(x) * 1000000), aux.y + int(Decimal(y) * 1000000))
shape.SetPolyShape(polygon)
board.Add(shape)
plot = board.GetPlotOptions()
selection = pcbnew.LSET()
for layer in EXPORT_LAYERS:
    layer_id = getattr(pcbnew, layer.replace('.', '_'), None)
    if layer_id is None:
        layer_id = board.GetLayerID(layer)
    assert layer_id >= 0
    selection.AddLayer(layer_id)
plot.SetLayerSelection(selection)
# Production was plotted on the drill/aux origin with silkscreen clipped at mask openings.
plot.SetUseAuxOrigin(True)
plot.SetSubtractMaskFromSilk(True)
plot.SetUseGerberProtelExtensions(True)
board.SetPlotOptions(plot)
pcbnew.SaveBoard(str(path), board)
# Runs in KiCad's bundled Python: no AL/X imports. Set both coverlay
# thicknesses inside the stackup block as a text change after SaveBoard.
source = path.read_text()
start = source.index('(stackup')
end = source.index('(copper_finish', start)
mask = re.compile(r'(\\(layer "[FB]\\.Mask"\\s+\\(type "[^"]*"\\)\\s+)\\(thickness [0-9.]+\\)')
block, count = mask.subn(lambda m: m.group(1) + '(thickness 0.05)\\n\\t\\t\\t\\t(material "PI coverlay + adhesive")'
                         + '\\n\\t\\t\\t\\t(epsilon_r 4)', source[start:end])
assert count == 2, count
path.write_text(source[:start] + block + source[end:])
print(json.dumps({'stiffener': moved_stiffener, 'notes': moved_notes,
                  'window': str(shape.m_Uuid.AsString())}))
'''
        script = ('SOURCE = ' + repr(str(SOURCE)) + '\nNOTE_LAYER = ' + repr(note_layer)
                  + '\nWINDOW = ' + repr(window) + '\nEXPORT_LAYERS = '
                  + repr(list(reference_layers)) + '\n' + script)
        edited = _succeeded(runtime, 'edit_kicad_work_copy', {
            'work_copy_id': work_id, 'script': script})
        evidence = json.loads(edited['stdout'])
        report = edited['change_report']['board']
        assert not report['truncated']
        assert report['stackup']['changed']
        moves = {item['id']: item for item in report['layer_moves']}
        # The stiffener zone is redrawn as a filled shape on User.1, so it shows
        # as one item added there and the B.Fab zone removed.
        added = {item['id']: item for item in report['items_added']}
        for item_id in evidence['stiffener']:
            assert added[item_id]['layers'] == ('User.1',)
        assert any(item['layers'] == ('B.Fab',) for item in report['items_removed'])
        for item_id in evidence['notes']:
            assert moves[item_id]['before'] == ('F.Mask',)
            assert moves[item_id]['after'] == (note_layer,)
        assert any(item['id'] == evidence['window'] and item['layers'] == ('F.Mask',)
                   for item in report['items_added'])
        exported = _succeeded(runtime, 'export_kicad_manufacturing', {
            'work_copy_id': work_id, 'output_dir': 'production', 'renames': renames,
            'separate_drills': True, 'drill_origin': 'plot',
            'compare_with': str(reference.resolve())})
        files = exported['files_written']
        assert any(Path(p).name == 'pib_0.15.gbr' for p in files)
        assert any(Path(p).suffix == '.drl' for p in files)
        assert all(Path(p).is_relative_to(tmp_path) for p in files)
        comparison = exported['comparison']
        assert not comparison['truncated']
        assert {row['name'] for row in comparison['files']} == set(previous)
        assert all(row['status'] == 'identical' and row['differing_lines'] == 0
                   for row in comparison['files']), [
                       (row['name'], row['status'], row['differing_lines'],
                        row['line_count_truncated']) for row in comparison['files']
                       if row['status'] != 'identical']
    finally:
        for path, stamp in stamps.items():
            try:
                current = _stamp(path)
            except OSError as error:
                pytest.skip(f'RoomReader input became inaccessible during verification: {path}: {error}')
            assert current == stamp, f'Original RoomReader input changed: {path}'
