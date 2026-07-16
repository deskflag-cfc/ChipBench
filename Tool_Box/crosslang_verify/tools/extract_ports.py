import re
import json


def _extract_module_body(code, module_name):
    """Return the text of `module_name`'s port-list parens, or None if not found.

    Files may contain multiple modules (submodules declared before the
    top-level module); we must only look at the named module's own port
    list, not whichever module happens to appear first in the file.
    """
    m = re.search(r'\bmodule\s+' + re.escape(module_name) + r'\b', code)
    if not m:
        return None

    pos = m.end()
    # Skip an optional #( ... ) parameter block before the port list.
    rest = code[pos:]
    hash_m = re.match(r'\s*#\s*\(', rest)
    if hash_m:
        depth = 0
        i = hash_m.end() - 1
        for i in range(hash_m.end() - 1, len(rest)):
            if rest[i] == '(':
                depth += 1
            elif rest[i] == ')':
                depth -= 1
                if depth == 0:
                    break
        pos += i + 1
        rest = code[pos:]

    paren_m = re.match(r'\s*\(', rest)
    if not paren_m:
        return None

    depth = 0
    start = paren_m.end() - 1
    end = None
    for i in range(start, len(rest)):
        if rest[i] == '(':
            depth += 1
        elif rest[i] == ')':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return None

    return rest[start + 1:end]


# Net/variable types and signing qualifiers that may sit between the direction
# and the port name. None of these is ever a port name itself.
_QUALIFIERS = ('reg', 'wire', 'logic', 'bit', 'var', 'signed', 'unsigned')

_PORT_RE = re.compile(
    r'\b(input|output|inout)\b'
    r'(?:\s+(?:' + '|'.join(_QUALIFIERS) + r')\b)*'   # zero or more qualifiers
    r'\s*(?:\[([^\]]*)\])?'                           # width: any expression
    r'\s*(\w+)'
)


def _collect_consts(code):
    """Map `define macros and parameters/localparams to their literal text.

    Widths in this corpus are written as [DATA_WIDTH-1:0] (cpu_ip params) and
    [`RegBus] (macros), so resolving a width needs both tables.
    """
    consts = {}
    for m in re.finditer(r'`define\s+(\w+)\s+([^\n]+)', code):
        consts[m.group(1)] = m.group(2).strip()
    for m in re.finditer(
            r'\b(?:parameter|localparam)\s+(?:\w+\s+)?(\w+)\s*=\s*([^,;)\n]+)', code):
        consts[m.group(1)] = m.group(2).strip()
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


def _width_of(width_expr, consts):
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
            f"unresolved width {width_expr!r} (expanded to {expanded!r}); "
            "the `define/parameter is not in scope of this file")
    if ':' not in expanded:
        return 1
    msb, lsb = expanded.split(':', 1)
    return abs(_eval_int(msb) - _eval_int(lsb)) + 1


def extract_ports_from_verilog(verilog_path, module_name="RefModule"):
    """Extract ports from Verilog. Returns (inputs, outputs) as [(name, width)]."""
    with open(verilog_path, 'r') as f:
        code = f.read()

    # Strip single-line and multi-line comments
    code = re.sub(r'//.*?$|/\*.*?\*/', '', code, flags=re.DOTALL | re.MULTILINE)

    body = _extract_module_body(code, module_name)
    if body is None:
        raise ValueError(f"Could not find module '{module_name}' in {verilog_path}")

    # Constants live outside the port list (param block, `defines above it).
    consts = _collect_consts(code)
    inputs, outputs = [], []

    for line in body.splitlines():
        line = line.strip().rstrip(',')
        m = _PORT_RE.match(line)
        if not m:
            continue
        direction, width_expr, name = m.groups()
        if name in _QUALIFIERS:
            raise ValueError(
                f"parsed a type keyword as a port name in {verilog_path}: {line!r}")
        width = _width_of(width_expr, consts)
        (inputs if direction == 'input' else outputs).append((name, width))

    return inputs, outputs

def extract_ports_from_json(json_path):
    """Extract ports from JSON. Returns (inputs, outputs) as [(name, width)]."""
    with open(json_path, 'r') as f:
        data = json.load(f)

    inputs = [(port['name'], port['width']) for port in data.get('inputs', [])]
    outputs = [(port['name'], port['width']) for port in data.get('outputs', [])]

    return inputs, outputs
