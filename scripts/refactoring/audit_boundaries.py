#!/usr/bin/env python3
"""Compare the approved baseline to the final modularized source without importing it."""
import argparse
import ast
import copy
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASELINE = '69388cc36a793f4eb86a79d46eb7b622e6115c61'
ENTRIES = ('tradingagents/execution/service.py', 'tradingagents/long_run.py')


def dump(node):
    return ast.dump(node, include_attributes=False)


def definitions(tree, prefix=''):
    """Include nested closures inside control-flow blocks in their lexical scope."""
    result = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = prefix + node.name
            result[name] = node
            result.update(definitions(node, name + '.'))
        else:
            result.update(definitions(node, prefix))
    return result


def body(node):
    return [dump(item) for item in node.body]


def expanded_daily_body(node, parsed):
    """Recompose the sole extracted contiguous symbol block, without normalizing behavior."""
    node = copy.deepcopy(node)
    # One reviewed behavior fix beyond extraction: reject corrupt symbol
    # states before recovery. Remove only this exact call for the baseline
    # comparison; report the exception explicitly, never normalize the helper.
    validation = ast.parse('_state.validate_resumable_symbols(journal, LongRunStop=LongRunStop)').body[0]
    for part in ast.walk(node):
        for field, value in ast.iter_fields(part):
            if isinstance(value, list):
                setattr(part, field, [item for item in value if not (
                    isinstance(item, ast.Expr) and dump(item) == dump(validation))])
    statements = []
    for item in node.body:
        if (isinstance(item, ast.Assign) and isinstance(item.value, ast.Call)
                and ast.unparse(item.value.func) == '_symbols.run_symbol_work'):
            helper = definitions(parsed['tradingagents/long_run_support/symbols.py'])['run_symbol_work']
            expected = {arg.arg for arg in helper.args.kwonlyargs}
            actual = {kw.arg for kw in item.value.keywords}
            if (expected != actual or item.value.args
                    or any(not isinstance(kw.value, ast.Name) or kw.value.id != kw.arg
                           for kw in item.value.keywords)):
                return None
            if not (isinstance(helper.body[-1], ast.Return)
                    and ast.unparse(helper.body[-1].value) == 'stop_reason'
                    and len(item.targets) == 1
                    and ast.unparse(item.targets[0]) == 'stop_reason'):
                return None
            statements.extend(helper.body[:-1])
        else:
            statements.append(item)
    return [dump(item) for item in statements]


def facts(node):
    calls, returns, raises, reads, writes, imports = set(), set(), set(), set(), set(), set()
    for part in ast.walk(node):
        if isinstance(part, ast.Call):
            calls.add(ast.unparse(part.func))
        elif isinstance(part, ast.Return):
            returns.add(ast.unparse(part.value) if part.value else 'None')
        elif isinstance(part, ast.Raise):
            raises.add(ast.unparse(part.exc) if part.exc else 're-raise')
        elif isinstance(part, ast.Attribute) and ast.unparse(part).startswith('self.'):
            (writes if isinstance(part.ctx, ast.Store) else reads).add(ast.unparse(part))
        elif isinstance(part, (ast.Import, ast.ImportFrom)):
            imports.add(ast.unparse(part))
    return dict(calls=sorted(calls), returns=sorted(returns), explicit_raises=sorted(raises),
                instance_reads=sorted(reads), instance_writes=sorted(writes), lazy_imports=sorted(imports))


def globals_of(tree):
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    result[target.id] = ast.unparse(node.value) if node.value else None
    return result


def signature(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ast.unparse(node.args), ast.unparse(node.returns) if node.returns else None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New audit JSON output path')
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error('Output already exists; use a new audit path')
    paths = sorted(set(ROOT.glob('tradingagents/execution/*.py')) |
                   set(ROOT.glob('tradingagents/long_run_support/*.py')) |
                   {ROOT / ENTRIES[1]})
    parsed = {str(path.relative_to(ROOT)): ast.parse(path.read_text()) for path in paths}
    current = {path: definitions(tree) for path, tree in parsed.items()}
    issues, records, state_records = [], [], []
    for path, tree in parsed.items():
        scopes = [('<module>', tree)] + [
            (node.name, node) for node in tree.body if isinstance(node, ast.ClassDef)
        ]
        for scope_name, scope in scopes:
            seen = set()
            for node in scope.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name in seen:
                        issues.append(f'{path}:{scope_name}: duplicate definition {node.name}')
                    seen.add(node.name)
    for entry in ENTRIES:
        original = ast.parse(subprocess.check_output(['git', 'show', f'{BASELINE}:{entry}'], cwd=ROOT, text=True))
        group = ('tradingagents/execution/' if entry == ENTRIES[0] else 'tradingagents/long_run_support/')
        owners = [p for p in current if p.startswith(group) or p == entry]
        for name, node in definitions(original).items():
            leaf = name.rsplit('.', 1)[-1]
            match = []
            for path in owners:
                for candidate_name, candidate in current[path].items():
                    if candidate_name not in (name, leaf) and candidate_name.rsplit('.', 1)[-1] != leaf:
                        continue
                    if type(candidate) is type(node) and (body(candidate) == body(node) or
                            (name == 'run_daily_round' and path == entry
                             and expanded_daily_body(candidate, parsed) == body(node))):
                        match.append((path, candidate_name, candidate.lineno))
            retained = current[entry].get(name)
            is_class = isinstance(node, ast.ClassDef)
            # A class alias also preserves its methods, without redefining a second class.
            if retained is None and '.' in name:
                parent, _ = name.split('.', 1)
                alias_exists = any(isinstance(part, ast.Assign) and isinstance(part.value, ast.Attribute)
                                   and any(isinstance(t, ast.Name) and t.id == parent for t in part.targets)
                                   for part in parsed[entry].body)
                if alias_exists and len(match) == 1:
                    retained = current[match[0][0]][match[0][1]]
            compatible = retained is not None and (is_class or signature(retained) == signature(node))
            # Nested functions were never module/class attributes. They preserve
            # their lexical signature and body at the unique implementation owner.
            nested = '.' in name and name.split('.')[0] not in {
                n.name for n in original.body if isinstance(n, ast.ClassDef)}
            nested = nested or name.count('.') >= 2
            if nested and len(match) == 1:
                candidate = current[match[0][0]][match[0][1]]
                compatible = signature(candidate) == signature(node)
            aliases = []
            if is_class and not retained:
                for part in parsed[entry].body:
                    if isinstance(part, ast.ImportFrom):
                        aliases += [ast.unparse(part) for alias in part.names if (alias.asname or alias.name) == leaf]
                    elif isinstance(part, ast.Assign) and isinstance(part.value, ast.Attribute):
                        if any(isinstance(t, ast.Name) and t.id == leaf for t in part.targets):
                            aliases.append(ast.unparse(part))
                compatible = bool(aliases)
            if not compatible:
                issues.append(f'{entry}:{name}: original entry/signature missing or changed')
            if not match and not (is_class and retained):
                issues.append(f'{entry}:{name}: no identical implementation body')
            if len(match) > 1:
                issues.append(f'{entry}:{name}: multiple identical implementation bodies')
            if (retained is not None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not nested):
                if [dump(d) for d in retained.decorator_list] != [dump(d) for d in node.decorator_list]:
                    issues.append(f'{entry}:{name}: decorators changed')
                if len(match) == 1 and match[0][0] != entry and body(retained) != body(node):
                    statements = retained.body[1:] if (retained.body and isinstance(retained.body[0], ast.Expr)
                        and isinstance(retained.body[0].value, ast.Constant)
                        and isinstance(retained.body[0].value.value, str)) else retained.body
                    call = statements[0].value if len(statements) == 1 and isinstance(statements[0], ast.Return) else None
                    if (len(statements) == 1 and isinstance(statements[0], ast.Expr)
                            and isinstance(statements[0].value, ast.YieldFrom)):
                        call = statements[0].value.value
                    imports = {a.asname or a.name: part.module + '.' + a.name
                        for part in parsed[entry].body if isinstance(part, ast.ImportFrom) and part.module
                        for a in part.names}
                    expected_owner = match[0][0][:-3].replace('/', '.')
                    expected_leaf = match[0][1].rsplit('.', 1)[-1]
                    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                            and isinstance(call.func.value, ast.Name)
                            and imports.get(call.func.value.id) == expected_owner
                            and call.func.attr == expected_leaf):
                        issues.append(f'{entry}:{name}: wrapper does not call its matched owner')
                    else:
                        helper = current[match[0][0]][match[0][1]]
                        passed = dict(zip([a.arg for a in helper.args.posonlyargs + helper.args.args], call.args))
                        passed.update({kw.arg: kw.value for kw in call.keywords})
                        for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                            if not isinstance(passed.get(arg.arg), ast.Name) or passed[arg.arg].id != arg.arg:
                                issues.append(f'{entry}:{name}: argument {arg.arg} not forwarded unchanged')
                        original_args = {a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}
                        for arg in helper.args.kwonlyargs:
                            if arg.arg not in original_args and (
                                    not isinstance(passed.get(arg.arg), ast.Name)
                                    or passed[arg.arg].id != arg.arg):
                                issues.append(f'{entry}:{name}: collaborator {arg.arg} not resolved at call time')
            if is_class and retained is not None:
                def class_state(cls):
                    return [dump(part) for part in cls.body
                            if not isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef))]
                if class_state(retained) != class_state(node):
                    issues.append(f'{entry}:{name}: class state or non-method statements changed')
            record = dict(original_file=entry, name=name, original_line=node.lineno,
                          original_end_line=node.end_lineno, signature=signature(node),
                          compatibility_signature_preserved=compatible,
                          alias=aliases, implementation_matches=match,
                          retained_line=retained.lineno if retained else None,
                          body_equivalent=bool(match) and name != 'run_daily_round',
                          baseline_flow_equivalent=bool(match),
                          reviewed_behavior_change=name == 'run_daily_round',
                          equivalence_kind=('recomposed symbol block, excluding reviewed corrupt-resume validation call'
                              if name == 'run_daily_round' and match else 'identical AST body'), kind='class' if is_class else 'function')
            record.update(facts(node))
            records.append(record)
        for name, value in globals_of(original).items():
            matches = [(p, globals_of(parsed[p])[name]) for p in owners
                       if name in globals_of(parsed[p])]
            state_records.append(dict(original_file=entry, name=name, original_value=value, current=matches))
            retained_value = globals_of(parsed[entry]).get(name)
            if retained_value != value:
                aliases = {a.asname or a.name: part.module + '.' + a.name
                    for part in parsed[entry].body if isinstance(part, ast.ImportFrom) and part.module
                    for a in part.names}
                resolved = None
                if retained_value and '.' in retained_value:
                    module_alias, attribute = retained_value.split('.', 1)
                    owner = aliases.get(module_alias, '').replace('.', '/') + '.py'
                    if owner in parsed:
                        resolved = globals_of(parsed[owner]).get(attribute)
                if resolved != value:
                    issues.append(f'{entry}:{name}: global initializer missing or changed')
            if name == '_stop_requested' and matches != [(entry, value)]:
                issues.append('long_run: stop flag must have one unchanged owner')
    back_imports = []
    for path, tree in parsed.items():
        if path in ENTRIES:
            continue
        if not (path.startswith('tradingagents/long_run_support/') or path in {
            'tradingagents/execution/' + name + '.py' for name in
            ('requests', 'order_planning', 'dispatch', 'protection', 'recovery', 'exits', 'intent_execution', 'contracts')
        }):
            continue
        for part in ast.walk(tree):
            if isinstance(part, (ast.Import, ast.ImportFrom)):
                text = ast.unparse(part)
                if ((path.startswith('tradingagents/execution/') and 'execution.service' in text)
                        or (path.startswith('tradingagents/long_run_support/')
                            and ('import long_run' in text or 'from tradingagents.long_run import' in text))):
                    back_imports.append((path, part.lineno, text))
    issues.extend(f'back import: {item}' for item in back_imports)
    report = dict(baseline=BASELINE, symbols=records, globals=state_records,
                  reviewed_behavior_changes=['run_daily_round rejects malformed resumable symbol maps/states before recovery; original silently skipped unknown states'],
                  structural_issues=issues, limitations=[
                      'Static syntax comparison complements, not replaces, behavioral differential tests.',
                      'Class bodies with delegated methods differ; retained class identity is tested at runtime.',
                      'run_daily_round is compared after exact re-expansion of its contiguous symbol block.',
                      'Nested closures are inventoried with their original bodies and signatures; they were never public attributes.',
                      'Call/return/raise/state lists include nested functions and are conservative source inventories.',
                      'Implicit exceptions and transitively invoked side effects are documented in boundary contracts.'
                  ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(symbols=len(records), identical_bodies=sum(r['body_equivalent'] for r in records),
                          baseline_flow_matches=sum(r['baseline_flow_equivalent'] for r in records),
                          issues=issues), indent=2))
    return bool(issues)


if __name__ == '__main__':
    raise SystemExit(main())
