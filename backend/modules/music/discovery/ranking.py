import math
import time
from . import identity, policy


BASIC_FEATURES={'language','vocal_role','vocals'}


def artist_preferences(choices):
    result={}
    for credit,weight in choices.items():
        members=identity.artist_members(credit)
        for name in members:
            result[name]=result.get(name,0)+weight/len(members)
    return result


def similarity(values, target):
    dimensions=[]
    for field, choices in target.items():
        value=values.get(field)
        positive={k:v for k,v in choices.items() if v>0}
        if value is None or value in ('','unknown') or not positive:
            continue
        if field=='artist':
            preferences=artist_preferences(positive)
            score=math.sqrt(max((preferences.get(name,0)/max(preferences.values())
                for name in identity.artist_members(value)),default=0)) if preferences else 0
        elif field in {'tempo','energy'}:
            mean=sum(float(k)*v for k,v in positive.items())/sum(positive.values())
            score=math.exp(-((float(value)-mean)/(35 if field=='tempo' else .3))**2)
        else:
            terms=value if isinstance(value,list) else [value]
            score=max((positive.get(str(v),0)/max(positive.values()) for v in terms),default=0)
        dimensions.append((field,score,.25 if field in BASIC_FEATURES else 1))
    if not dimensions: return None
    result=sum(s*w for _,s,w in dimensions)/sum(w for _,_,w in dimensions)
    fields={field for field,_,_ in dimensions}
    if fields<=BASIC_FEATURES: return min(result,.3)
    if fields<=BASIC_FEATURES|{'artist'}:
        return min(result,.65 if fields=={'artist'} else .75)
    return result


def score(track, profile, intent):
    analysis=track.get('analysis',{})
    from .profile import trusted_features
    values={'artist':track.get('artist','')}|trusted_features(analysis.get('features',{}),analysis.get('evidence',{}))
    if analysis.get('evidence',{}).get('fields',{}).get('vocals',{}).get('confidence',0):
        values['vocals']=analysis.get('features',{}).get('vocals')
    settings=profile['settings']
    members=identity.artist_members(values['artist'])
    if any(members & identity.artist_members(credit) for credit in settings['excluded_artists']+intent.get('excluded_artists',[])):
        return None
    languages=intent.get('languages') or settings['languages']
    if languages and values.get('language') not in languages:
        return None
    if intent.get('vocals')=='instrumental' and values.get('vocals') is not False: return None
    energy=intent.get('desired_energy')
    if energy is not None and (values.get('energy') is None or abs(values['energy']-energy)>.3): return None
    if intent.get('moods') and not set(intent['moods']) & set(values.get('moods') or []): return None
    long_values={f:dict(v) for f,v in profile['long_term'].get('features',{}).items()}
    short_values={f:dict(v) for f,v in profile['short_term'].get('features',{}).items()}
    explicit={}
    for item in settings.get('confirmed_profile_items',[]):
        if item['scope']=='context' and item.get('context')!=intent.get('context','general'): continue
        explicit.setdefault(item['dimension'],{})[item['value']]=item['preference']
    long_values.update(explicit);short_values.update(explicit)
    long=similarity(values,long_values)
    short=similarity(values,short_values)
    # Confirmed dimensions take precedence; inferred overlap contributes at most 20%.
    agent_values={};agent_refs=[]
    if profile.get('agent_fusion_enabled',True):
        for item in profile.get('agent_state',{}).get('items',[]):
            if item.get('source')!='inferred' or item.get('confidence',0)<.6 or item['dimension'] in explicit: continue
            if item.get('expires_at') and item['expires_at']<=time.time(): continue
            if item['scope']=='context' and item.get('context')!=intent.get('context','general'): continue
            agent_values.setdefault(item['dimension'],{})[item['value']]=item['preference']*item['confidence']
            agent_refs.extend(item.get('evidence_refs',[]))
    inferred=similarity(values,agent_values)
    if inferred is not None:
        long=.8*long+.2*inferred if long is not None else inferred
    for field,choices in explicit.items():
        terms=values.get(field);terms=terms if isinstance(terms,list) else [terms]
        if field=='artist':
            if any(weight<0 and members & identity.artist_members(credit) for credit,weight in choices.items()): return None
        elif any(choices.get(str(v),0)<0 for v in terms): return None
    seed_scores=[]
    for seed_track in profile.get('seeds',[]):
        seed_values={'artist':seed_track.get('artist','')}|{field:value for field,value in seed_track.get('features',{}).items()
            if field in BASIC_FEATURES|{'energy','tempo','moods','themes','style','instrument'} and value not in (None,'','unknown',[])}
        musical={field for field in seed_values if field not in BASIC_FEATURES|{'artist'}}
        if not musical and not members & identity.artist_members(seed_values['artist']):
            continue
        target={field:{str(term):1 for term in value if term is not None} if isinstance(value,list) else {str(value):1}
            for field,value in seed_values.items() if value is not None and value not in ('','unknown')}
        seed_scores.append(similarity(values,target))
    seed=max((v for v in seed_scores if v is not None),default=None)
    evidence={field:item for field,item in analysis.get('evidence',{}).get('fields',{}).items() if field in values}
    confidence=sum(float(v.get('confidence',0)) for v in evidence.values())/len(evidence) if evidence else .2 if long is not None else 0
    explicit_match=(languages and values.get('language') in languages or
        any(members & identity.artist_members(credit) for credit in intent.get('preferred_artists',[])) or
        intent.get('vocals')=='instrumental' and values.get('vocals') is False or energy is not None)
    # Explicit requirements can support exploration; inferred language alone cannot.
    if explicit_match and (long is None or long<.55): long=.55
    if long is None and short is None and seed is None:
        return None
    parts=[(long,.55),(short,.20),(seed,.15),(confidence,.10)]
    match=sum(v*w for v,w in parts if v is not None)/sum(w for v,w in parts if v is not None)
    if match<policy.MIN_SCORE:
        return None
    refs=list(dict.fromkeys(profile['long_term'].get('evidence_refs',[])[:3]+agent_refs[:3]))
    if not refs: refs=['intent:current_batch']
    musical=any(field not in BASIC_FEATURES|{'artist'} for field in values)
    familiar=any(members & identity.artist_members(credit) for credit,weight in long_values.get('artist',{}).items() if weight>0)
    reason=('基于喜欢的歌手推荐，其他特征证据有限' if familiar and not musical else
        '符合当前明确条件的探索推荐' if explicit_match and not musical else
        '与所选种子或收藏歌曲的已知特征接近' if seed is not None else '与已有偏好证据匹配')
    return {'match_score':round(match,4),'reason':reason,'evidence_refs':refs+['features:'+track['key']],
            'components':{'long_term':long,'short_term':short,'seed_similarity':seed,'confidence':confidence}}


def select(tracks, target=policy.TARGET, exploration=.2, familiar_artists=(), exposure=None, retained=()):
    exposure=exposure or {}
    remaining=list(tracks); selected=[]; artists={}; recordings=set(); works=set()
    familiar={name for credit in familiar_artists for name in identity.artist_members(credit)
        if not isinstance(familiar_artists,dict) or familiar_artists[credit]>0}
    individual_exposure={}
    for credit,count in exposure.items():
        for name in identity.artist_members(credit):
            individual_exposure[name]=individual_exposure.get(name,0)+count
    def recording(t): return t.get('_recording_group',t['recording_key'])
    def artist(t): return identity.artist_members(t.get('artist',''))
    def novel(t): return bool(artist(t)) and not artist(t)&familiar
    def eligible(t):
        return (all(artists.get(name,0)<2 for name in artist(t)) and recording(t) not in recordings
            and (not t['work_key'] or t['work_key'] not in works)
            and not any(identity.same_recording(t,other) for other in selected))
    def append(track):
        selected.append(track)
        for name in artist(track): artists[name]=artists.get(name,0)+1
        recordings.add(recording(track));works.add(track['work_key'])
    for track in retained:
        if len(selected)<target and eligible(track):
            append(track|{'selection':track['selection']|{'exploration':novel(track)}})
    while remaining and len(selected)<target:
        choices=[t for t in remaining if eligible(t)]
        if not choices: break
        previous=artist(selected[-1]) if selected else frozenset()
        alternating=[t for t in choices if not artist(t)&previous]
        if alternating: choices=alternating
        exploration_slot=sum(novel(t) for t in selected)<round(target*exploration)
        unfamiliar=[t for t in choices if novel(t)]
        if exploration_slot and unfamiliar: choices=unfamiliar
        chosen=max(choices,key=lambda t:t['selection']['match_score']-.15*max((artists.get(name,0) for name in artist(t)),default=0)
                   -.2*bool(previous&artist(t))-.02*min(5,max((individual_exposure.get(name,0) for name in artist(t)),default=0))
                   +.01*t['selection'].get('agent_order_bonus',0))
        chosen['selection']['exploration']=novel(chosen)
        remaining.remove(chosen); append(chosen)
    return selected


def selectable_count(tracks):
    """Count usable reserves as complete six-song batches plus the next partial batch."""
    from . import identity
    remaining=[track|{'selection':dict(track['selection'])} for track in tracks]
    count=0
    while remaining:
        selected=select(remaining)
        count+=len(selected)
        if len(selected)<policy.TARGET:
            break
        recordings={track.get('_recording_group',track['recording_key']) for track in selected}
        works={track['work_key'] for track in selected if track['work_key']}
        remaining=[track for track in remaining
            if track.get('_recording_group',track['recording_key']) not in recordings
            and (not track['work_key'] or track['work_key'] not in works)
            and not any(identity.same_recording(track,other) for other in selected)]
    return count
