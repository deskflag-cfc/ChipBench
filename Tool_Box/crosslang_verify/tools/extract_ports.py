import glob
import json
import os
import re


# Net/variable types and signing qualifiers that may sit between the direction
# and the port name. None of these is ever a port name itself.
_QUALIFIERS = ('reg', 'wire', 'logic', 'bit', 'var', 'signed', 'unsigned')

# A declaration head: direction, optional qualifiers, optional width, remainder.
# The width is captured as a raw expression -- [31:0], [`RegBus] and
# [DATA_WIDTH-1:0] all have to survive to _width_of() for resolution.
_DECL_RE = re.compile(
    r'^\s*(input|output|inout)\b'
    r'((?:\s+(?:' + '|'.join(_QUALIFIERS) + r')\b)*)'
    r'\s*(?:\[([^\]]*)\])?'
    r'\s*(.*)$',
    re.DOTALL)

_NAME_RE = re.compile(r'^\s*([A-Za-z_]\w*)')


def _strip_comments(code):
    return re.sub(r'//.*?$|/\*.*?\*/', '', code, flags=re.DOTALL | re.MULTILINE)


def _match_paren(text, open_idx):
    """Index of the ')' matching the '(' at open_idx, or None."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return i
    return None


def _find_module(code, module_name):
    """Return (port_list_text, body_text) for module_name, or (None, None).

    Files may contain submodules declared before the top-level module, so we
    anchor on the named module rather than whichever appears first. An
    optional #( ... ) parameter block may sit between the name and the port
    list -- `module RefModule#(parameter W=8)(input [W-1:0] a, ...)`.
    """
    m = re.search(r'\bmodule\s+' + re.escape(module_name) + r'\b', code)
    if not m:
        return None, None

    pos = m.end()
    rest = code[pos:]

    hash_m = re.match(r'\s*#\s*\(', rest)
    if hash_m:
        end = _match_paren(rest, hash_m.end() - 1)
        if end is None:
            return None, None
        pos += end + 1
        rest = code[pos:]

    paren_m = re.match(r'\s*\(', rest)
    if not paren_m:
        # A module with no port list at all: `module foo; ... endmodule`
        return '', rest.split('endmodule', 1)[0]

    start = paren_m.end() - 1
    end = _match_paren(rest, start)
    if end is None:
        return None, None

    port_list = rest[start + 1:end]
    body = rest[end + 1:].split('endmodule', 1)[0]
    return port_list, body


def _collect_consts(code):
    """Map `define macros and parameters/localparams to their literal text."""
    consts = {}
    for m in re.finditer(r'`define\s+(\w+)\s+([^\n]+)', code):
        consts[m.group(1)] = m.group(2).strip()
    for m in re.finditer(
            r'\b(?:parameter|localparam)\s+(?:\w+\s+)?(\w+)\s*=\s*([^,;)\n]+)', code):
        consts[m.group(1)] = m.group(2).strip()
    return consts


def _define_sources(verilog_path):
    """Sibling files that may carry `defines this file uses but doesn't define.

    The cpu_ip references declare ports as [`RegBus] / [`InstAddrBus] while the
    `define lives in the companion _test.sv. Verilog `defines are global to a
    compilation unit, so reading the sibling mirrors what the simulator sees.
    """
    directory = os.path.dirname(verilog_path) or '.'
    base = os.path.basename(verilog_path)
    sources = []

    for suffix in ('_ref.sv', '_ref.v'):
        if base.endswith(suffix):
            stem = base[:-len(suffix)]
            for cand in (stem + '_test.sv', stem + '_test.v'):
                path = os.path.join(directory, cand)
                if os.path.exists(path):
                    sources.append(path)
            break

    for pattern in ('*.vh', '*.svh'):
        sources.extend(sorted(glob.glob(os.path.join(directory, pattern))))
    return sources


def _build_consts(verilog_path, code):
    """Constants from sibling define sources, overridden by this file's own."""
    consts = {}
    for path in _define_sources(verilog_path):
        try:
            with open(path, 'r') as f:
                consts.update(_collect_consts(_strip_comments(f.read())))
        except OSError:
            continue
    consts.update(_collect_consts(code))
    return consts


def _expand(text, consts, depth=0):
    """Substitute macros and parameters until the text stops changing."""
    if depth > 16:
        return text
    out = re.sub(r'`(\w+)', lambda m: consts.get(m.group(1), m.group(0)), text)
    out = re.sub(r'\b([A-Za-z_]\w*)\b',
                 lambda m: consts.get(m.group(1), m.group(0)), out)
    return _expand(out, consts, depth + 1) if out != text else out


def _eval_int(expr):
    if not re.fullmatch(r'[0-9\s+\-*/()]+', expr):
        raise ValueError(f"non-numeric width term: {expr!r}")
    return int(eval(expr))  # charset-restricted above


def _width_of(width_expr, consts, path):
    """Resolve a [msb:lsb] expression to a bit count.

    Refuse to guess: an unresolved macro silently yielding a 1-bit port is far
    worse than a hard error, because it produces a testbench that compiles
    against the wrong widths and reports bogus mismatches.
    """
    if width_expr is None:
        return 1
    expanded = _expand(width_expr, consts).strip()
    if '`' in expanded or re.search(r'[A-Za-z_]', expanded):
        raise ValueError(
            f"unresolved width {width_expr!r} (expanded to {expanded!r}) in {path}; "
            "the `define/parameter is not in scope of this file or its siblings")
    if ':' not in expanded:
        return 1
    msb, lsb = expanded.split(':', 1)
    return abs(_eval_int(msb) - _eval_int(lsb)) + 1


def _split_top_commas(text):
    """Split on commas that are not nested inside brackets or parens."""
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    if ''.join(cur).strip():
        out.append(''.join(cur))
    return out


def _parse_decls(text, consts, path):
    """Parse comma-separated declarations into [(direction, name, width)].

    `input [1:0] d0, d1, d2` declares three 2-bit inputs: a segment with no
    direction of its own inherits the direction and width of the preceding
    one. Dropping that rule silently loses every port after the first.
    """
    ports = []
    cur_dir = None
    cur_width = None

    for seg in _split_top_commas(text):
        if not seg.strip():
            continue
        m = _DECL_RE.match(seg)
        if m:
            cur_dir = m.group(1)
            cur_width = m.group(3)
            rest = m.group(4)
        else:
            rest = seg
        if cur_dir is None:
            # A bare name in a non-ANSI header port list; its direction is
            # declared in the module body and picked up there.
            continue
        nm = _NAME_RE.match(rest)
        if not nm:
            continue
        name = nm.group(1)
        if name in _QUALIFIERS:
            raise ValueError(
                f"parsed a type keyword as a port name in {path}: {seg.strip()!r}")
        ports.append((cur_dir, name, _width_of(cur_width, consts, path)))
    return ports


def _parse_body_decls(body, consts, path):
    """Parse non-ANSI declarations that live in the module body."""
    ports = []
    for stmt in body.split(';'):
        if not re.match(r'^\s*(input|output|inout)\b', stmt.strip()):
            continue
        ports.extend(_parse_decls(stmt, consts, path))
    return ports


def extract_ports_from_verilog(verilog_path, module_name="RefModule"):
    """Extract ports from Verilog. Returns (inputs, outputs) as [(name, width)].

    An `inout` is reported in both lists: the testbench must drive it and
    compare it.
    """
    with open(verilog_path, 'r') as f:
        code = f.read()

    code = _strip_comments(code)

    port_list, body = _find_module(code, module_name)
    if port_list is None:
        raise ValueError(f"Could not find module '{module_name}' in {verilog_path}")

    consts = _build_consts(verilog_path, code)

    # ANSI ports live in the header; non-ANSI ones in the body. Parsing both
    # also supports mixed-style sources. First declaration of a name wins.
    decls = _parse_decls(port_list, consts, verilog_path)
    decls += _parse_body_decls(body, consts, verilog_path)

    inputs, outputs, seen = [], [], set()
    for direction, name, width in decls:
        if name in seen:
            continue
        seen.add(name)
        if direction in ('input', 'inout'):
            inputs.append((name, width))
        if direction in ('output', 'inout'):
            outputs.append((name, width))

    return inputs, outputs


def extract_ports_from_json(json_path):
    """Extract ports from JSON. Returns (inputs, outputs) as [(name, width)]."""
    with open(json_path, 'r') as f:
        data = json.load(f)

    inputs = [(port['name'], port['width']) for port in data.get('inputs', [])]
    outputs = [(port['name'], port['width']) for port in data.get('outputs', [])]

    return inputs, outputs
