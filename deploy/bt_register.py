#!/usr/bin/env python3
"""Register this checkout through installed BaoTa's native Python-project model.

Run only with /www/server/panel/pyenv/bin/python3 on the target server.
Existing unrelated projects are neither touched nor restarted. Dependencies must
already be installed in the dedicated virtualenv. Registration skips BaoTa's
optional web-server dependency installer because these are command projects;
startup/stop/daemon/logs otherwise use its native model without panel changes.
"""
import grp
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time

BASE = Path('/www/wwwroot/mk-quant')
PYTHON = Path('/www/server/pyporject_evn/mk-quant/bin/python')
NAMES = ('api','backtest','runtime')

LAUNCHER = '''"""BaoTa command entrypoint; one independently managed process per role."""
import os
from pathlib import Path
import resource
import sys
base=Path(__file__).resolve().parents[2]
role=Path(__file__).resolve().parent.name
# BaoTa may cache environment values at registration. Read the private role
# file on every process start so configuration changes actually take effect.
for line in (base/"config"/(role+".env")).read_text().splitlines():
    line=line.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line=line[7:].strip()
    key,separator,value=line.partition("=")
    if not separator or not key.strip().isidentifier():
        raise RuntimeError("Invalid role environment configuration")
    value=value.strip()
    if len(value)>1 and value[0]==value[-1] and value[0] in (chr(34),chr(39)):
        value=value[1:-1]
    os.environ[key.strip()]=value
os.environ.pop("MK_DB_PATH",None)
os.chdir(str(base))
os.environ["PYTHONPATH"]=str(base)
os.environ["MK_ENABLE_LIVE"]="0"
os.environ["MK_DEMO"]="0"
os.environ["PYTHONUNBUFFERED"]="1"
if hasattr(os,"sched_getaffinity"):
    available=sorted(os.sched_getaffinity(0))
    if len(available)>1 and role in {"backtest","runtime"}:
        os.sched_setaffinity(0,{available[-1] if role=="backtest" else available[0]})
if role=="backtest":
    os.nice(10)
    ceiling=int(os.getenv("MK_BACKTEST_ADDRESS_SPACE_MB","768"))*1024*1024
    resource.setrlimit(resource.RLIMIT_AS,(ceiling,ceiling))
if role!="api":
    os.environ.pop("MK_MASTER_KEY",None)
    argv=[sys.executable,"-m","backend.worker",role]
else:
    argv=[sys.executable,"-m","uvicorn","backend.app:app","--host","127.0.0.1","--port","8765","--proxy-headers","--forwarded-allow-ips","127.0.0.1"]
os.execv(sys.executable,argv)
'''


def ensure_user():
    account=pwd.getpwnam('mkquant')
    before={'uid':account.pw_uid,'gid':account.pw_gid}
    if account.pw_uid<1000:
        used_uids={u.pw_uid for u in pwd.getpwall()}
        used_gids={g.gr_gid for g in grp.getgrall()}
        new_id=next(i for i in range(1000,60000) if i not in used_uids and i not in used_gids)
        subprocess.run(['groupmod','-g',str(new_id),'mkquant'],check=True)
        subprocess.run(['usermod','-u',str(new_id),'-g','mkquant','mkquant'],check=True)
        # Fix only this project's ownership. Root-owned secrets stay root owned.
        for parent,dirs,files in os.walk(str(BASE)):
            for entry in [parent]+[os.path.join(parent,f) for f in files]:
                stat=os.lstat(entry)
                os.lchown(entry,new_id if stat.st_uid==before['uid'] else -1,new_id if stat.st_gid==before['gid'] else -1)
    return before,pwd.getpwnam('mkquant')


def main():
    if os.geteuid()!=0 or not PYTHON.exists() or not (BASE/'backend'/'app.py').exists():
        raise RuntimeError('Run as root only after code and virtualenv are deployed')
    os.umask(0o027)
    state=BASE/'deploy'/'state';state.mkdir(parents=True,exist_ok=True)
    before,account=ensure_user()
    for role in NAMES:
        service=BASE/'services'/role;service.mkdir(parents=True,exist_ok=True)
        launcher=service/'launcher.py';launcher.write_text(LAUNCHER)
        os.chown(str(service),account.pw_uid,account.pw_gid)
        os.chown(str(launcher),account.pw_uid,account.pw_gid)
        os.chmod(str(launcher),0o640)
        if not (BASE/'config'/(role+'.env')).is_file():
            raise RuntimeError('Missing role environment file: '+role)
    os.chdir('/www/server/panel')
    sys.path.insert(0,'/www/server/panel')
    sys.path.insert(0,'/www/server/panel/class')
    import public
    from mod.project.python.pyenv_tool import EnvironmentManager
    from projectModel.pythonModel import main as PythonModel
    result=EnvironmentManager.add_python_env('venv',str(PYTHON.parent.parent))
    if result:
        raise RuntimeError(result)
    model=PythonModel()
    # Keep the official validation + insertion path; this one instance skips
    # unrelated uwsgi/gunicorn/daphne installations for command-only services.
    model.run_simple_prep_env=lambda project_id,conf:(True,'Dedicated environment already prepared')
    before_projects=[]
    for role in NAMES:
        item=model.get_project_find('mk-quant-'+role)
        if item:
            before_projects.append(item)
    backup=state/('bt-python-before-'+str(int(time.time()))+'.json')
    backup.write_text(json.dumps({'user_before':before,'projects':before_projects},ensure_ascii=False,default=str))
    os.chmod(str(backup),0o600)
    output=[]
    for role in NAMES:
        name='mk-quant-'+role
        existing=model.get_project_find(name)
        if existing and existing['path']!=str(BASE/'services'/role):
            raise RuntimeError('Existing project name has another owner/path: '+name)
        if not existing:
            get=public.dict_obj()
            for key,value in {
                'pjname':name,'port':'','stype':'command',
                'path':str(BASE/'services'/role),'user':'mkquant',
                'python_bin':str(PYTHON),'project_cmd':'python launcher.py',
                'env_file':str(BASE/'config'/(role+'.env')),
                'env_list':[],'framework':'python','auto_run':True,
                'requirement_path':'','rfile':'','initialize':''
            }.items():
                setattr(get,key,value)
            created=model.CreateProject(get)
            if not created.get('status'):
                raise RuntimeError(json.dumps(created,ensure_ascii=False))
        get=public.dict_obj();get.name=name
        model.StartProject(get)
        model._pids=None
        item=model.get_project_find(name)
        pids=model.get_project_run_state(name)
        output.append({'name':name,'id':item['id'],'pids':pids,'auto_run':item['project_config']['auto_run'],'path':item['path']})
        if not pids:
            raise RuntimeError('Project failed to start; inspect BaoTa Python project log: '+name)
    print(json.dumps({'projects':output,'uid':account.pw_uid,'backup':str(backup)},ensure_ascii=False))


if __name__=='__main__':
    main()
