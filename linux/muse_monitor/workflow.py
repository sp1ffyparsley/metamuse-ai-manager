"""Preparation outside browser ownership, with explicit evidence and identity."""
import hashlib,json,os,re,time
from pathlib import Path
from .store import Conflict,Denied

def prepare_packet(monitor,actor,body):
    from .server import private_data
    data=body.get('packet',{});private_data(data)
    required={'canonicalItem','kind','applicant','sourceFacts','sourceTimestamps','candidateFacts','eligibility','documents','draftAnswers','questions','nextSafeAction'}
    if not isinstance(data,dict) or not required<=data.keys():raise Conflict('Complete preparation packet and evidence required')
    if data['kind'] not in ('jobs','scholarships','research','tech'):raise Conflict('Explicit task type required')
    facts=data['candidateFacts']
    if not isinstance(facts,list) or any(not isinstance(f,dict) or not {'value','source','version','verification','approvalScope'}<=f.keys() for f in facts):raise Conflict('Candidate facts need source/version/verification/approval provenance')
    if any(f['verification'] not in ('verified','unresolved','conflicting') for f in facts):raise Conflict('Unknown fact verification state')
    questions=list(data['questions'])
    professional=monitor.config.get('applicationIdentity',{}).get('primaryEmail')
    if professional and data['kind'] in ('jobs','scholarships'):
        contact=data.get('contactEmail')
        if not isinstance(contact,str) or contact.casefold()!=professional.casefold():questions.append('Confirm the configured professional application email; a Chrome/Drive account is not the applicant contact identity.')
    if not facts:questions.append('Verified candidate facts and source provenance are required.')
    expected={'jobs':{'employer','requisition','description','officialURL','eligibility'},'scholarships':{'sponsor','program','cycle','eligibility','deadline','writingRules','obligations','requirements'}}.get(data['kind'],set())
    source=data['sourceFacts']
    if not isinstance(source,dict):raise Conflict('Structured posting/sponsor facts required')
    missing=expected-set(source)
    if missing:questions.append('Missing source facts: '+', '.join(sorted(missing)))
    if not data['sourceTimestamps']:questions.append('Source retrieval/version evidence is required.')
    by_path={}
    for fact in facts:
        if fact.get('path'):by_path.setdefault(fact['path'],set()).add(json.dumps(fact['value'],sort_keys=True))
    if any(len(v)>1 for v in by_path.values()):questions.append('Conflicting approved-source values require reconciliation; do not guess.')
    data=dict(data,questions=questions,schema=1,preparedBy=actor['id'],preparedAt=time.time())
    version=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    folder=monitor.root/'packets';folder.mkdir(mode=0o700,exist_ok=True)
    file=folder/(version+'.json');file.write_text(json.dumps(data,indent=2));file.chmod(0o600)
    return {'packet':version,'version':version,'ready':not data['questions'] and all(f['verification']=='verified' for f in facts),'nextSafeAction':data['nextSafeAction']}

def deadline(value,zone,precision='date'):
    """Preserve a deadline's stated meaning; no invented midnight cutoff."""
    if precision not in ('date','instant','forecast','unknown'):raise ValueError('Deadline precision required')
    if not zone and precision=='instant':raise ValueError('Exact deadline requires source timezone')
    return {'original':value,'timezone':zone,'precision':precision,'displayTimezone':'America/New_York','exactCutoff':value if precision=='instant' else None}

def match_challenge(messages,service,recipient,started,task):
    """Never select an unrelated latest six-digit number."""
    candidates=[m for m in messages if m.get('service')==service and m.get('recipient','').casefold()==recipient.casefold() and m.get('receivedAt',0)>=started and m.get('task')==task and not m.get('used')]
    if len(candidates)!=1:raise Conflict('Verification challenge is absent or ambiguous')
    return candidates[0].get('code')

def generate_pdf_artifact(state,approved,filename):
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xml.sax.saxutils import escape
    if not {'title','paragraphs','sources','applicant','approved'}<=approved.keys() or approved['approved'] is not True:raise ValueError('Approved sourced content required')
    if not approved['sources'] or not approved['paragraphs']:raise ValueError('Real source/content required')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}\.pdf',filename):raise ValueError('Safe PDF filename required')
    folder=Path(state)/'artifacts';folder.mkdir(mode=0o700,exist_ok=True)
    path=folder/filename;styles=getSampleStyleSheet()
    # Embed known metrics rather than relying on viewer substitution of the
    # standard Helvetica font, which rendered poorly in the VM's PDF stack.
    if 'MuseSans' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('MuseSans','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
        pdfmetrics.registerFont(TTFont('MuseSansBold','/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))
    styles['Title'].fontName='MuseSansBold';styles['Title'].fontSize=19;styles['Title'].leading=25
    styles['BodyText'].fontName='MuseSans';styles['BodyText'].fontSize=10;styles['BodyText'].leading=15
    story=[Paragraph(escape(approved['title']),styles['Title']),Spacer(1,12)]
    for p in approved['paragraphs']:story.extend([Paragraph(escape(p),styles['BodyText']),Spacer(1,8)])
    SimpleDocTemplate(str(path),pagesize=letter,rightMargin=54,leftMargin=54,topMargin=54,bottomMargin=54).build(story)
    path.chmod(0o600)
    return {'artifact':filename,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size,'sources':approved['sources'],'applicant':approved['applicant']}
