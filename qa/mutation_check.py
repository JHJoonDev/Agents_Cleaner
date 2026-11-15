"""Fresh-process AST mutation checks. Never rewrite production sources.

Reports are atomic files in reports/mutation only. Cache reuse requires matching
production, inventory, runner AND test hashes. Workers always load outer QA guards.
"""
from __future__ import annotations
import argparse
import ast
import copy
import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT=Path(__file__).resolve().parents[1]
REPORT_DIR=ROOT/'reports/mutation'
COMMON=['tests/test_native_guard.py','tests/test_sandbox_guard.py','tests/test_safety_contracts.py','tests/test_writable_review.py']
MANAGER=['tests/test_manager.py','tests/test_codex_path_safety.py','tests/test_direct_delete.py','tests/test_runtime_safety.py::MutationSafetyTests','tests/test_adversarial.py']
GROUPS={
 'guard-paths':('manager/model.py',{'is_link','_link_stat','inside','safe_path','signature'},90,MANAGER+['tests/test_guard_regressions.py']),
 'guard-validation':('manager/operations.py',{'_no_links','_checked_nodes','validate','validate_project_folder','_stopped'},90,MANAGER+['tests/test_guard_regressions.py','tests/test_artifact_regressions.py','tests/test_review_survivors.py']),
 'guard-deletion':('manager/operations.py',{'_remove_artifact','remove_backups','delete'},90,MANAGER+['tests/test_artifacts.py','tests/test_guard_regressions.py','tests/test_artifact_regressions.py','tests/test_native_regressions.py','tests/test_review_survivors.py']),
 'guard-workspaces':('manager/workspaces.py',{'fixed_problem','workspace_problem'},90,MANAGER+['tests/test_workspace_artifacts.py','tests/test_workspace_regressions.py']),
 'guard-artifacts':('manager/artifacts.py',{'validate_shape','revalidate','live_problem','process_problem'},90,['tests/test_artifacts.py','tests/test_project_sizes.py','tests/test_artifact_regressions.py','tests/test_guard_regressions.py']),
 'guard-project-size':('manager/project_sizes.py',{'recycle_candidate','protected'},90,['tests/test_project_sizes.py','tests/test_size_regressions.py']),
 'size-thresholds':('manager/project_sizes.py',{'scan_project','scan_roots','cache_name','measure'},85,['tests/test_project_sizes.py','tests/test_size_regressions.py','tests/test_root_file_candidate.py']),
 'read-only':('manager/qt_logic.py',{'review','submit_review','stop_apps'},90,['tests/test_safety_properties.py::test_readonly_entrypoints_cannot_launch_deletion_or_process_shutdown']),
 'size-read-only':('manager/qt_sizes.py',{'review'},90,['tests/test_safety_properties.py::test_readonly_entrypoints_cannot_launch_deletion_or_process_shutdown']),
 'shutdown':('manager/app_control.py',{'_alive','remaining','request_close','force_stop','protected_pids'},85,[]),
 'guard-recycle':('manager/recycle.py',{'recycle'},90,['tests/test_recycle_contracts.py','tests/test_com_failure_candidate.py']),
}
FLIPS={ast.Eq:ast.NotEq,ast.NotEq:ast.Eq,ast.Is:ast.IsNot,ast.IsNot:ast.Is,
 ast.In:ast.NotIn,ast.NotIn:ast.In,ast.Lt:ast.LtE,ast.LtE:ast.Lt,
 ast.Gt:ast.GtE,ast.GtE:ast.Gt,ast.And:ast.Or,ast.Or:ast.And,
 ast.Add:ast.Sub,ast.Sub:ast.Add,ast.Mult:ast.Div}

def variants(tree,functions,readonly=False):
    selected=set()
    for fn in ast.walk(tree):
        if isinstance(fn,ast.FunctionDef) and fn.name in functions:
            if readonly:
                for n in ast.walk(fn):
                    if isinstance(n,ast.If) and 'allow_delete' in ast.unparse(n.test):
                        selected.update(id(x) for x in ast.walk(n.test))
                        selected.add(id(n))
            else:selected.update(id(x) for x in ast.walk(fn))
    for index,node in enumerate(ast.walk(tree)):
        if id(node)not in selected:continue
        if isinstance(node,(ast.Compare,ast.BoolOp,ast.BinOp)):
            ops=node.ops if isinstance(node,ast.Compare)else[node.op]
            for position,op in enumerate(ops):
                if type(op)in FLIPS:yield index,'operator',position,FLIPS[type(op)].__name__
        if isinstance(node,ast.UnaryOp)and isinstance(node.op,ast.Not):yield index,'not',0,'remove not'
        if isinstance(node,ast.Constant)and type(node.value)in{bool,int,float}:
            yield index,'constant',0,repr(not node.value if isinstance(node.value,bool)else node.value+1)
        if isinstance(node,ast.If):
            yield index,'condition',0,'always true'
            yield index,'condition',1,'always false'

def mutated(tree,variant):
    index,kind,position,_=variant;tree=copy.deepcopy(tree);node=list(ast.walk(tree))[index]
    if kind=='operator':
        if isinstance(node,ast.Compare):node.ops[position]=FLIPS[type(node.ops[position])]()
        else:node.op=FLIPS[type(node.op)]()
    elif kind=='not':
        replacement=node.operand;node.__class__=replacement.__class__;node.__dict__=replacement.__dict__.copy()
    elif kind=='condition':node.test=ast.Constant(value=position==0)
    else:node.value=not node.value if isinstance(node.value,bool)else node.value+1
    return ast.fix_missing_locations(tree)

def catalog():
    result=[]
    for group,(relative,names,threshold,tests)in GROUPS.items():
        path='scripts/'+relative;tree=ast.parse((ROOT/path).read_text(encoding='utf8'));nodes=list(ast.walk(tree))
        for variant in variants(tree,names,'read-only'in group):
            node=nodes[variant[0]]
            result.append({'id':len(result),'group':group,'path':path,'line':node.lineno,
                'expression':ast.unparse(node)[:180],'variant':list(variant)})
    return result

def fingerprint(items):
    hashes={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ['scripts','tests','qa']for p in (ROOT/folder).rglob('*')if p.is_file()and p.suffix in{'.py','.json'}}
    for relative in ['qa_pytest_guard.py','pytest.ini']:
        hashes[relative]=hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()
    hashes['inventory']=hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()
    return hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(),hashes

def report_path(name):
    # One plain filename: no absolute path, drive-relative name, ADS, traversal or nested roots.
    if Path(name).name!=name or ':'in name or name in{'.','..'}or not name.endswith('.json'):
        raise ValueError('output must be one .json filename inside reports/mutation')
    for part in [REPORT_DIR,*REPORT_DIR.parents]:
        if part.exists()and(part.is_symlink()or getattr(part.lstat(),'st_file_attributes',0)&0x400):
            raise ValueError('report ancestors cannot be links/junctions')
    target=REPORT_DIR/name
    if target.is_symlink()or target.exists()and(getattr(target.lstat(),'st_file_attributes',0)&0x400):
        raise ValueError('report cannot replace a link/junction')
    if target.resolve().parent!=REPORT_DIR.resolve():raise ValueError('report outside dedicated directory')
    return target

def write_report(name,data):
    target=report_path(name);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.tmp-'+str(os.getpid()))
    # O_EXCL refuses a preexisting temporary link; replace never follows a destination link.
    with temporary.open('x',encoding='utf8')as stream:json.dump(data,stream,indent=2)
    report_path(name)
    temporary.replace(target)

def worker(item):
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(ROOT))
    import qa_pytest_guard  # Guard must precede pytest import and test collection.
    if item is not None:
        path=(ROOT/item['path']).resolve();tree=ast.parse(path.read_text(encoding='utf8'))
        try:code=compile(mutated(tree,item['variant']),str(path),'exec')
        except (SyntaxError,ValueError,TypeError)as exc:
            print('QA_BUILD_ERROR:',exc)
            return 3
        original=importlib.machinery.SourceFileLoader.get_code
        def load(loader,fullname):
            return code if Path(loader.path).resolve()==path else original(loader,fullname)
        importlib.machinery.SourceFileLoader.get_code=load
    import pytest
    tests=list(dict.fromkeys(COMMON+(GROUPS[item['group']][3]if item else [t for _,_,_,ts in GROUPS.values()for t in ts])))
    os.environ['HYPOTHESIS_PROFILE']='ci'
    return int(pytest.main(['-p','qa_pytest_guard','-x','-q','--tb=short',*tests]))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',type=int);parser.add_argument('--worker-data');parser.add_argument('--output',default='results.json')
    parser.add_argument('--ids');parser.add_argument('--groups');parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--timeout',type=float,default=60);parser.add_argument('--catalog',action='store_true');parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    if args.worker_data:
        item=json.loads(args.worker_data)
        if item['path']!='scripts/'+GROUPS[item['group']][0]:raise ValueError('invalid worker target')
        return worker(item)
    if args.worker==-1:return worker(None)
    items=catalog()
    if args.worker is not None:return worker(items[args.worker]if args.worker>=0 else None)
    if not 1<=args.workers<=4:parser.error('workers must be 1..4')
    path=report_path(args.output) # Validate BEFORE baseline or any app import.
    sha,hashes=fingerprint(items)
    if args.catalog:
        print(json.dumps({g:sum(x['group']==g for x in items)for g in GROUPS},indent=2));return 0
    if args.groups:
        groups=set(args.groups.split(','));unknown=groups-GROUPS.keys()
        if unknown:parser.error('unknown groups: '+str(unknown))
        items=[x for x in items if x['group']in groups]
    if args.ids:
        ids=set(map(int,args.ids.split(',')));items=[x for x in items if x['id']in ids]
    payload={'fingerprint':sha,'hashes':hashes,'selected':[x['id']for x in items],'results':[],'thresholds':{g:v[2]for g,v in GROUPS.items()}}
    if args.resume and path.exists():
        previous=json.loads(path.read_text(encoding='utf8'))
        if previous.get('fingerprint')!=sha:raise ValueError('stale mutation cache: source/test/runner/inventory hash mismatch')
        payload['results']=previous['results']
    startup=subprocess.run([sys.executable,'-B',str(ROOT/'qa/check_guard_startup.py')],cwd=ROOT,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
    if startup.returncode:
        print(startup.stdout[-2000:]+startup.stderr[-2000:]);raise RuntimeError('early guard/cold import controls failed')
    baseline=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker','-1'],cwd=ROOT,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
    if baseline.returncode:
        print(baseline.stdout[-6000:]+baseline.stderr[-2000:]);raise RuntimeError('unmutated sandboxed baseline failed')
    print('Guarded baseline passed; starting fresh-process mutants',flush=True)
    completed={x['id']for x in payload['results']};pending=[x for x in items if x['id']not in completed]
    def run(item):
        start=time.monotonic()
        try:
            result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker-data',json.dumps(item)],cwd=ROOT,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=args.timeout)
            status='Survived'if result.returncode==0 else 'Killed'if result.returncode in{1,2}else'Error'
            detail=(result.stdout+result.stderr)[-2400:]if status!='Survived'else''
        except subprocess.TimeoutExpired:status,detail='Timeout','owned Python worker exceeded deadline'
        return dict(item,status=status,detail=detail,seconds=round(time.monotonic()-start,3))
    with ThreadPoolExecutor(max_workers=args.workers)as pool:
        for future in as_completed([pool.submit(run,item)for item in pending]):
            payload['results'].append(future.result());write_report(args.output,payload)
            if len(payload['results'])%20==0:print(f'{len(payload["results"])}/{len(items)} mutants complete',flush=True)
    if fingerprint(catalog())[0]!=sha:raise RuntimeError('inputs changed during mutation run; report is incomplete and cannot certify current sources')
    payload['complete']=True;write_report(args.output,payload)
    failed=False
    for group in GROUPS:
        rows=[r for r in payload['results']if r['group']==group]
        if not rows:continue
        counts={s:sum(r['status']==s for r in rows)for s in ['Killed','Timeout','Survived','Error']}
        score=100*(counts['Killed']+counts['Timeout'])/len(rows)
        print(group,json.dumps(counts),f'{score:.2f}%',flush=True)
        if score<GROUPS[group][2]or counts['Error']:failed=True
    return int(failed)

if __name__=='__main__':raise SystemExit(main())
