"""Reviewed provider-neutral skill baseline for every Legion executor."""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, tarfile, tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
GRAPHIFY_VERSION='0.9.43'
PINNED_SOURCES={'ponytail': ('https://github.com/DietrichGebert/ponytail.git', '2ed6c52c9d7e5e56942508591085fd45dea277d3'), 'caveman': ('https://github.com/JuliusBrussee/caveman.git', '27d5a3981a347890211bb1bf2439e5c821a63bc9'), 'agent-skills': ('https://github.com/addyosmani/agent-skills.git', 'df1edb2e05487d0aa6d93c747141e0aed1187f25'), 'impeccable': ('https://github.com/pbakaus/impeccable.git', '7b646bafd60b9dd9828ce5c4c1a25691702c9e92'), 'taste': ('https://github.com/Leonxlnx/taste-skill.git', 'e988add20dab0fa97d7a76781c48961c8184288e'), 'emil': ('https://github.com/emilkowalski/skills.git', '78761e1b57f97dce65b983d640c70a68f39e8163'), 'matt': ('https://github.com/mattpocock/skills.git', '8b78b531ab965735c5dc74f6f7a219e1e37326df')}
EXPECTED_SKILLS=('animate', 'animation-vocabulary', 'api-and-interface-design', 'apple-design', 'ask-matt', 'ask-sonner', 'brandkit', 'browser-testing-with-devtools', 'brutalist-skill', 'cavecrew', 'caveman', 'caveman-commit', 'caveman-compress', 'caveman-help', 'caveman-review', 'caveman-stats', 'ci-cd-and-automation', 'code-review', 'code-review-and-quality', 'code-simplification', 'codebase-design', 'context-engineering', 'debugging-and-error-recovery', 'deprecation-and-migration', 'diagnosing-bugs', 'documentation-and-adrs', 'domain-modeling', 'doubt-driven-development', 'emil-design-eng', 'find-animation-opportunities', 'frontend-ui-engineering', 'git-workflow-and-versioning', 'gpt-tasteskill', 'graphify', 'grill-me', 'grill-with-docs', 'grilling', 'handoff', 'idea-refine', 'image-to-code-skill', 'imagegen-frontend-mobile', 'imagegen-frontend-web', 'impeccable', 'implement', 'improve-animations', 'improve-codebase-architecture', 'incremental-implementation', 'interview-me', 'minimalist-skill', 'observability-and-instrumentation', 'output-skill', 'performance-optimization', 'pick-ui-library', 'planning-and-task-breakdown', 'ponytail', 'ponytail-audit', 'ponytail-debt', 'ponytail-gain', 'ponytail-help', 'ponytail-review', 'prototype', 'redesign-skill', 'research', 'resolving-merge-conflicts', 'review-animations', 'security-and-hardening', 'setup-matt-pocock-skills', 'shipping-and-launch', 'soft-skill', 'source-driven-development', 'spec-driven-development', 'stitch-skill', 'taste-skill', 'taste-skill-v1', 'tdd', 'teach', 'test-driven-development', 'to-questionnaire', 'to-spec', 'to-tickets', 'triage', 'using-agent-skills', 'wait-what', 'wayfinder', 'wizard', 'writing-for-agents')
REVIEWED_CAVEMAN_SKILLS=frozenset({'caveman', 'caveman-help', 'caveman-review', 'caveman-commit', 'caveman-compress', 'cavecrew', 'caveman-stats'})
FORBIDDEN_HOOK_PATTERNS=('simplify-ignore', 'sdd-cache-')
@dataclass(frozen=True)
class SkillCheck:
    root:str; ok:bool; missing:tuple[str,...]=(); unexpected:tuple[str,...]=(); forbidden_hooks:tuple[str,...]=()
def _expand(v:str)->Path: return Path(os.path.expandvars(os.path.expanduser(v))).resolve()
def _skills(root:Path)->set[str]: return {p.name for p in root.iterdir() if p.is_dir() and p.name!='.system' and (p/'SKILL.md').is_file()} if root.is_dir() else set()
def verify_root(root:Path)->SkillCheck:
    got=_skills(root); exp=set(EXPECTED_SKILLS); hooks=[]
    if root.is_dir():
        for p in root.rglob('*'):
            if p.is_file() and any(p.name.startswith(x) for x in FORBIDDEN_HOOK_PATTERNS): hooks.append(str(p))
    return SkillCheck(str(root),got==exp and not hooks,tuple(sorted(exp-got)),tuple(sorted(got-exp)),tuple(sorted(hooks)))
def verify_roots(roots:Iterable[Path])->list[SkillCheck]: return [verify_root(r) for r in roots]
def roots_for_executor(registry:object, executor:object)->list[Path]:
    vals=list(getattr(executor,'skill_roots',()) or ())
    if not vals:
        rt=getattr(registry,'runtimes',{}).get(getattr(executor,'runtime','')); vals=list(getattr(rt,'skill_roots',()) or ())
    return [_expand(v) for v in vals]
def roots_from_registry(registry:object)->list[Path]:
    seen=set(); out=[]
    for ex in getattr(registry,'executors',{}).values():
        for r in roots_for_executor(registry,ex):
            if r not in seen: seen.add(r); out.append(r)
    return out
def select_stage_skills(text:str, required:Iterable[str]=(), *, limit:int=3)->tuple[str,...]:
    chosen=[]; exp=set(EXPECTED_SKILLS)
    for s in required:
        if s in exp and s not in chosen: chosen.append(s)
        if len(chosen)>=limit:return tuple(chosen)
    t=text.casefold(); rules=[
      (('security','crypto','auth','trust'),('security-and-hardening','code-review','doubt-driven-development')),
      (('review','audit','verify'),('code-review','ponytail-review','caveman-review')),
      (('test','validation','regression'),('test-driven-development','tdd','triage')),
      (('research','paper','literature'),('research','source-driven-development','context-engineering')),
      (('architecture','design','migration'),('improve-codebase-architecture','codebase-design','deprecation-and-migration')),
      (('performance','benchmark','scale'),('performance-optimization','observability-and-instrumentation','research')),
      (('plan','roadmap','scope'),('planning-and-task-breakdown','to-tickets','handoff')),
      (('implement','build','code','patch'),('implement','incremental-implementation','code-simplification'))]
    for keys, skills in rules:
        if any(k in t for k in keys):
            for s in skills:
                if s in exp and s not in chosen: chosen.append(s)
                if len(chosen)>=limit:return tuple(chosen)
    if not chosen: chosen=['context-engineering','handoff']
    return tuple(chosen[:limit])
def render_skill_context(roots:Iterable[Path], skills:Iterable[str], *, max_chars_per_skill:int=12000)->str:
    roots=[r.expanduser() for r in roots]; blocks=[]
    for name in list(skills)[:3]:
        src=next((r/name/'SKILL.md' for r in roots if (r/name/'SKILL.md').is_file()),None)
        if not src: raise RuntimeError(f'missing selected skill {name!r}')
        text=src.read_text(encoding='utf-8',errors='replace')[:max_chars_per_skill]
        blocks.append(f'## ACTIVE REVIEWED SKILL: {name}\n\n{text}')
    return '\n\n'.join(blocks)
def _run(cmd:list[str],cwd:Path|None=None,env:dict[str,str]|None=None):
    cp=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True,check=False)
    if cp.returncode: raise RuntimeError(f"command failed: {' '.join(cmd)}\n{cp.stderr or cp.stdout}")
def _clone(url:str,pin:str,dst:Path):
    _run(['git','clone','--quiet','--filter=blob:none','--no-checkout',url,str(dst)]); _run(['git','fetch','--quiet','--depth','1','origin',pin],dst); _run(['git','checkout','--quiet','--detach',pin],dst)
def _copy(src:Path,dst:Path):
    if not (src/'SKILL.md').is_file(): return
    if dst.exists(): shutil.rmtree(dst)
    shutil.copytree(src,dst)
def _build(stage:Path)->Path:
    clones={}
    for name,(url,pin) in PINNED_SOURCES.items(): clones[name]=stage/name; _clone(url,pin,clones[name])
    profile=stage/'profile'; profile.mkdir()
    for label in ('ponytail','caveman','agent-skills','taste','emil'):
        d=clones[label]/'skills'
        if not d.is_dir(): continue
        for s in d.iterdir():
            if not (s/'SKILL.md').is_file():continue
            if label=='caveman' and s.name not in REVIEWED_CAVEMAN_SKILLS:continue
            if label=='emil' and s.name=='prototype': pass
            elif (profile/s.name).exists(): continue
            _copy(s,profile/s.name)
            if label=='agent-skills' and (clones[label]/'references').is_dir() and 'references/' in (s/'SKILL.md').read_text(encoding='utf-8',errors='replace'):
                shutil.copytree(clones[label]/'references',profile/s.name/'references',dirs_exist_ok=True)
    for d in (clones['matt']/'skills'/'engineering',clones['matt']/'skills'/'productivity'):
        if d.is_dir():
            for s in d.iterdir():
                if s.name!='prototype': _copy(s,profile/s.name)
    _copy(clones['impeccable']/'.agents'/'skills'/'impeccable',profile/'impeccable')
    graphify=shutil.which('graphify')
    version_ok=False
    if graphify:
        cp=subprocess.run([graphify,'--version'],text=True,capture_output=True,check=False); version_ok=cp.returncode==0 and GRAPHIFY_VERSION in (cp.stdout+cp.stderr)
    if not version_ok:
        pipx=shutil.which('pipx')
        if not pipx: raise RuntimeError(f'Graphify {GRAPHIFY_VERSION} is required; install pipx or the reviewed Graphify version')
        _run([pipx,'install','--force',f'graphifyy=={GRAPHIFY_VERSION}']); graphify=shutil.which('graphify')
    if not graphify: raise RuntimeError(f'Graphify {GRAPHIFY_VERSION} did not become available')
    home=stage/'graphify-home'; home.mkdir(); env=dict(os.environ); env['HOME']=str(home)
    _run([graphify,'install','--platform','agents'],env=env); _copy(home/'.agents'/'skills'/'graphify',profile/'graphify')
    got=_skills(profile); exp=set(EXPECTED_SKILLS)
    if got!=exp: raise RuntimeError(f'staged skill baseline mismatch missing={sorted(exp-got)} unexpected={sorted(got-exp)}')
    return profile
def install_roots(roots:Iterable[Path], *, backup_root:Path|None=None)->list[SkillCheck]:
    roots=[r.expanduser() for r in roots]; backup_root=backup_root or Path.home()/'.local/share/hermes-legion-commander/skill-backups'; backup_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hlc-skills-') as tmp:
        profile=_build(Path(tmp))
        for root in roots:
            root.mkdir(parents=True,exist_ok=True)
            if any(root.iterdir()):
                with tarfile.open(backup_root/(root.parent.name+'-skills.tar.gz'),'w:gz') as tar: tar.add(root,arcname=root.name)
            for p in list(root.iterdir()):
                if p.is_dir() and p.name!='.system' and p.name not in EXPECTED_SKILLS: shutil.rmtree(p)
            for s in profile.iterdir(): _copy(s,root/s.name)
    return verify_roots(roots)
def manifest()->dict[str,object]: return {'schema_version':1,'graphify_version':GRAPHIFY_VERSION,'skill_count':len(EXPECTED_SKILLS),'skills':list(EXPECTED_SKILLS),'sources':{k:{'url':u,'commit':c} for k,(u,c) in PINNED_SOURCES.items()},'forbidden_hooks':list(FORBIDDEN_HOOK_PATTERNS)}
def cli_main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(prog='hermes-legion-commander skills'); sub=p.add_subparsers(dest='command',required=True); sub.add_parser('manifest')
    for name in ('check','install'):
        q=sub.add_parser(name); q.add_argument('--root',action='append',default=[]); q.add_argument('--config',type=Path)
    a=p.parse_args(argv)
    if a.command=='manifest': print(json.dumps(manifest(),indent=2,sort_keys=True)); return 0
    roots=[_expand(v) for v in a.root]
    if a.config:
        from .legion_config import load
        roots += roots_from_registry(load(a.config).registry)
    roots=list(dict.fromkeys(roots))
    if not roots: p.error('check/install requires --root or --config')
    checks=install_roots(roots) if a.command=='install' else verify_roots(roots)
    print(json.dumps([asdict(c) for c in checks],indent=2,sort_keys=True)); return 0 if all(c.ok for c in checks) else 1
