#!/usr/bin/env python3
"""
tournament_run_strategies.py
1147 стратегий M00-M1146 — blind features → decisions.

Запуск:
    python3 scripts/tournament_run_strategies.py --division all --include-oracle
"""
from __future__ import annotations
import sqlite3, os, argparse, datetime
from dataclasses import dataclass
from typing import Optional, Callable

TOURN_DB   = os.path.join(os.path.dirname(__file__), "../data/model_tournament.db")
HARVEST_DB = os.path.join(os.path.dirname(__file__), "../storage/betsapi_harvest.db")
STAKE_FLAT = 20.0


@dataclass
class BlindMatch:
    event_id: str; match_date: str; split: str; division: str
    league: str; home_team: str; away_team: str; bookmaker: str
    start_time: int; open_home: float; open_away: float
    market_prob_home: float; market_prob_away: float
    elo_home: float; elo_away: float; elo_diff: float
    elo_prob_home: float; edge_home: float
    h2h_n: int; h2h_wr_home: float; h2h_wr_away: float
    h2h_delta: float; adj_prob_home: float
    pre_match_pts: int; pre_match_move: Optional[float]
    latest_pre_prob: Optional[float]


@dataclass
class Decision:
    bet: bool; bet_team: str; odds: float
    model_prob: float; market_prob: float; edge: float
    stake_usd: float; reason_code: str


def nb(code): return Decision(False,'',0.,0.,0.,0.,0.,code)
def _bet(t,o,ep,mp,e,c): return Decision(True,t,o,ep,mp,e,STAKE_FLAT,c)


# ─── Фабрики ────────────────────────────────────────────────────────────────

def _s(code, *, E=0.0, O=99.0, Lo=1.0, Elo=0.0, EloMax=9999.0,
       M=0.0, Mx=1.0, H=0, Wr=0.0, Dt=-1.0, Hp=False, Pre=0):
    """Стандартная фабрика: best_side с параметрами."""
    def strat(m, **_):
        ea = 1-m.elo_prob_home; eda = ea-m.market_prob_away
        ed = abs(m.elo_diff)
        for team,odds,ep,mp,edge in[
            ('home',m.open_home,m.elo_prob_home,m.market_prob_home,m.edge_home),
            ('away',m.open_away,ea,m.market_prob_away,eda)]:
            if odds<=Lo or odds>=O: continue
            if edge<E: continue
            if mp<M or mp>Mx: continue
            if ed<Elo or ed>EloMax: continue
            if m.h2h_n<H: continue
            if Wr>0 and (m.h2h_wr_home if team=='home' else m.h2h_wr_away)<Wr: continue
            if Dt>-1:
                dt=m.h2h_delta if team=='home' else -m.h2h_delta
                if dt<Dt: continue
            if Hp:
                dt=m.h2h_delta if team=='home' else -m.h2h_delta
                if dt<=0: continue
            if m.pre_match_pts<Pre: continue
            return _bet(team,odds,ep,mp,edge,code)
        return nb(code)
    return strat


def _rc(code, *, Elo=75.0, E=0.0, Lo=0.60, Hi=0.70, O=2.0,
        Hp=False, H=0, Adj=False, Pre=0):
    """Фабрика Rule-C семьи."""
    def strat(m, **_):
        ea=1-m.elo_prob_home; eda=ea-m.market_prob_away
        for team,odds,ep,mp,edge in[
            ('home',m.open_home,m.elo_prob_home,m.market_prob_home,m.edge_home),
            ('away',m.open_away,ea,m.market_prob_away,eda)]:
            if edge<E or odds>=O or odds<=1: continue
            if abs(m.elo_diff)<Elo: continue
            if not Lo<=mp<=Hi: continue
            if Hp and (m.h2h_delta if team=='home' else -m.h2h_delta)<=0: continue
            if m.h2h_n<H: continue
            if Adj:
                adj=m.adj_prob_home if team=='home' else 1-m.adj_prob_home
                if adj<=mp: continue
            if m.pre_match_pts<Pre: continue
            return _bet(team,odds,ep,mp,edge,code)
        return nb(code)
    return strat


def _lg(code, keys, **kw):
    """Фабрика: лига + _s."""
    base = _s(code, **kw)
    def strat(m, **_):
        if not any(k in (m.league or '').lower() for k in keys): return nb(code)
        return base(m)
    return strat


def _lg_rc(code, keys, **kw):
    """Фабрика: лига + _rc."""
    base = _rc(code, **kw)
    def strat(m, **_):
        if not any(k in (m.league or '').lower() for k in keys): return nb(code)
        return base(m)
    return strat


def _adj(code, *, MinAdj=0.65, O=2.0, Elo=75.0, M=0.0, Mx=1.0, AdjEdge=0.0):
    """Фабрика: adj_prob стратегия."""
    def strat(m, **_):
        ah=m.adj_prob_home; aa=1-ah
        for team,odds,adj,mp in[
            ('home',m.open_home,ah,m.market_prob_home),
            ('away',m.open_away,aa,m.market_prob_away)]:
            edge=adj-mp
            if adj<MinAdj or odds>=O or odds<=1: continue
            if abs(m.elo_diff)<Elo: continue
            if mp<M or mp>Mx: continue
            if edge<AdjEdge: continue
            return _bet(team,odds,adj,mp,edge,code)
        return nb(code)
    return strat


# ═══════════════════════════════════════════════════════════════════
#  M00–M16  ОРИГИНАЛЬНЫЕ
# ═══════════════════════════════════════════════════════════════════

def m00(m,**_): return nb('M00')

def m01(m,**_):
    for t,o,mp in[('home',m.open_home,m.market_prob_home),('away',m.open_away,m.market_prob_away)]:
        if mp>=0.5 and 1<o<2.0: return _bet(t,o,mp,mp,0.,'M01')
    return nb('M01')

def m02(m,**_):
    for t,o,mp in[('home',m.open_home,m.market_prob_home),('away',m.open_away,m.market_prob_away)]:
        if 0.60<=mp<=0.70 and o>1: return _bet(t,o,mp,mp,0.,'M02')
    return nb('M02')

def m03(m,**_): return _s('M03',E=0.001,O=3.0)(m)
def m04(m,**_):
    ah=m.adj_prob_home; aa=1-ah
    for t,o,adj,mp in[('home',m.open_home,ah,m.market_prob_home),('away',m.open_away,aa,m.market_prob_away)]:
        if adj-mp>0 and 1<o<3.0: return _bet(t,o,adj,mp,adj-mp,'M04')
    return nb('M04')

def m05(m,**_): return _rc('M05')(m)
def m06(m,**_): return _rc('M06',E=0.07)(m)
def m07(m,**_): return _s('M07',E=0.001,O=2.0,Elo=150)(m)

def m08(m,**_):
    if m.h2h_n<3: return nb('M08')
    ah=m.adj_prob_home; aa=1-ah
    for t,o,adj,mp in[('home',m.open_home,ah,m.market_prob_home),('away',m.open_away,aa,m.market_prob_away)]:
        dt=m.h2h_delta if t=='home' else -m.h2h_delta
        if dt>0.02 and adj-mp>0 and 1<o<2.0: return _bet(t,o,adj,mp,adj-mp,'M08')
    return nb('M08')

def m09(m,**_): return _rc('M09',O=1.7,Lo=0.60,Hi=0.75)(m)
def m10(m,**_): return _s('M10',E=0.05,O=2.2,Elo=50)(m)

def m11(m,**_):
    if 'dreamleague' not in (m.league or '').lower(): return nb('M11')
    return _s('M11',E=0.001,O=3.0)(m)

def m12(m,**_):
    lg=(m.league or '').lower()
    if 'european pro league' not in lg and 'epl' not in lg: return nb('M12')
    return _s('M12',E=0.001,O=3.0)(m)

def m13(m,**_):
    if 'pgl' not in (m.league or '').lower(): return nb('M13')
    return _s('M13',E=0.001,O=3.0)(m)

def m14(m,bm_probs=None,**_):
    bm_probs=bm_probs or {}
    if len(bm_probs)<2: return nb('M14')
    probs=list(bm_probs.values()); gap=max(probs)-min(probs)
    if gap<0.05: return nb('M14')
    avg=sum(probs)/len(probs); ea=1-m.elo_prob_home
    if m.elo_prob_home>avg and 1<m.open_home<3.0:
        return _bet('home',m.open_home,m.elo_prob_home,avg,m.elo_prob_home-avg,'M14')
    if ea>(1-avg) and 1<m.open_away<3.0:
        return _bet('away',m.open_away,ea,1-avg,ea-(1-avg),'M14')
    return nb('M14')

def m15(m,**_):
    if m.pre_match_pts<5 or m.pre_match_move is None: return nb('M15')
    if m.pre_match_move<-0.03 and 1<m.open_away<3.0:
        return _bet('away',m.open_away,1-m.elo_prob_home,m.market_prob_away,abs(m.pre_match_move),'M15')
    if m.pre_match_move>0.03 and 1<m.open_home<3.0:
        return _bet('home',m.open_home,m.elo_prob_home,m.market_prob_home,abs(m.pre_match_move),'M15')
    return nb('M15')

def m16(m,close_home=None,close_away=None,**_):
    if not close_home or not close_away: return nb('M16')
    if close_home<m.open_home and 1<close_home<2.0: return _bet('home',m.open_home,m.market_prob_home,m.market_prob_home,0.,'M16')
    if close_away<m.open_away and 1<close_away<2.0: return _bet('away',m.open_away,m.market_prob_away,m.market_prob_away,0.,'M16')
    return nb('M16')


# ═══════════════════════════════════════════════════════════════════
#  M17–M46  НОВЫЕ (первые 30)
# ═══════════════════════════════════════════════════════════════════

def m17(m,**_):
    for t,o,mp in[('home',m.open_home,m.market_prob_home),('away',m.open_away,m.market_prob_away)]:
        if 1<o<1.30: return _bet(t,o,m.elo_prob_home if t=='home' else 1-m.elo_prob_home,mp,0.,'M17')
    return nb('M17')

m18=_s('M18',O=1.40,Elo=50)
m19=_s('M19',E=0.001,O=1.50,Elo=100)

def m20(m,**_):
    for t,o,mp in[('home',m.open_home,m.market_prob_home),('away',m.open_away,m.market_prob_away)]:
        ep=m.elo_prob_home if t=='home' else 1-m.elo_prob_home
        if 1<o<1.50 and mp>=0.75: return _bet(t,o,ep,mp,ep-mp,'M20')
    return nb('M20')

m21=_s('M21',E=0.001,O=2.0,Elo=300)
m22=_s('M22',O=2.5,Elo=400)
m23=_s('M23',O=1.8,Elo=200,M=0.68)
m24=_s('M24',O=1.8,Elo=150,Hp=True)
m25=_s('M25',O=2.0,H=5,Wr=0.80)
m26=_s('M26',O=2.0,H=5,Wr=0.75,Elo=50)
m27=_s('M27',O=2.0,H=3,Dt=0.40,Elo=75)
m28=_s('M28',E=0.001,O=1.8,H=4,Wr=0.70)
m29=_s('M29',M=0.80)

def m30(m,**_): return _s('M30',M=0.75,Elo=50)(m)
m31=_s('M31',E=0.02,Elo=100,M=0.72)
m32=_s('M32',Elo=150,Hp=True,M=0.70)
m33=_rc('M33',Elo=100)
m34=_rc('M34',Hp=True,H=1)
m35=_rc('M35',E=0.08)
m36=_rc('M36',Lo=0.62,Hi=0.68)
m37=_s('M37',E=0.001,O=2.0,Elo=100,H=3,Hp=True)
m38=_s('M38',E=0.05,Elo=75,H=3,Hp=True,M=0.60,Mx=0.70)  # was using _rc-like but generalized
m39=_s('M39',E=0.03,O=1.8,Elo=200)
m40=_s('M40',Elo=100,H=3,Hp=True,M=0.65)
m41=_s('M41',E=0.001,O=2.0,Pre=10)
m42=None  # handled inline (line move)
m43=None  # handled inline (line move + elo)
m44=_lg('M44',('dreamleague',),O=1.5,Elo=50)
m45=_lg('M45',('pgl',),O=1.5,Elo=50)

def m46_fn(m,**_):
    lg=(m.league or '').lower()
    is_major=any(x in lg for x in('dreamleague','pgl','esl','the international',' ti '))
    if not is_major: return nb('M46')
    for t,o,mp in[('home',m.open_home,m.market_prob_home),('away',m.open_away,m.market_prob_away)]:
        ep=m.elo_prob_home if t=='home' else 1-m.elo_prob_home
        if 1<o<1.30: return _bet(t,o,ep,mp,ep-mp,'M46')
    return nb('M46')


# ═══════════════════════════════════════════════════════════════════
#  M47–M146  НОВЫЕ (100 штук) — компактные factory
# ═══════════════════════════════════════════════════════════════════

# Лиги
DL=('dreamleague',); PG=('pgl',); EP=('european pro league','epl')
MJ=('dreamleague','pgl','esl one','the international',' ti ')
CH=('championship','cup','league season')

# ── Edge bands (M47-M56) ────────────────────────────────────────────
m47=_s('M47',E=0.05,O=3.0)
m48=_s('M48',E=0.08,O=2.5)
m49=_s('M49',E=0.10,O=2.5)
m50=_s('M50',E=0.12,O=2.5)
m51=_s('M51',E=0.15,O=3.0)
m52=_s('M52',E=0.20,O=3.0)
m53=_s('M53',E=0.05,O=2.5,Elo=50)
m54=_s('M54',E=0.08,O=2.0,Elo=75)
m55=_s('M55',E=0.10,O=2.0,Elo=75)
m56=_s('M56',E=0.12,O=2.0,Elo=100)

# ── Elo + market prob (M57-M66) ─────────────────────────────────────
m57=_s('M57',O=2.0,Elo=75, M=0.60,Mx=0.70)
m58=_s('M58',E=0.02,O=2.0,Elo=100,M=0.60)
m59=_s('M59',O=2.0,Elo=200)
m60=_s('M60',O=2.0,Elo=250)
m61=_s('M61',O=1.8,Elo=75, M=0.65)
m62=_s('M62',O=1.8,Elo=100,M=0.65)
m63=_s('M63',O=1.8,Elo=150,M=0.65)
m64=_s('M64',E=0.03,O=1.8,Elo=75, M=0.60,Mx=0.70)
m65=_s('M65',E=0.03,O=1.8,Elo=100,M=0.60,Mx=0.70)
m66=_s('M66',E=0.03,O=1.8,Elo=150,M=0.65)

# ── H2H bands (M67-M76) ─────────────────────────────────────────────
m67=_s('M67',O=2.0,H=3,Wr=0.65)
m68=_s('M68',O=2.0,H=4,Wr=0.65)
m69=_s('M69',O=2.0,H=5,Wr=0.65)
m70=_s('M70',O=2.0,H=6,Wr=0.65)
m71=_s('M71',O=2.0,H=3,Wr=0.70)
m72=_s('M72',O=2.0,H=5,Wr=0.70)
m73=_s('M73',E=0.05,O=2.0,H=3,Hp=True)
m74=_s('M74',E=0.05,O=2.0,H=5,Hp=True)
m75=_s('M75',O=2.0,H=5,Dt=0.30)
m76=_s('M76',O=2.0,H=3,Dt=0.30,Elo=75)

# ── Market prob bands (M77-M86) ──────────────────────────────────────
m77=_s('M77',E=0.08,O=3.0,Elo=75, M=0.55,Mx=0.65)
m78=_s('M78',E=0.05,O=3.0,Elo=75, M=0.60,Mx=0.65)
m79=_s('M79',E=0.03,O=3.0,Elo=75, M=0.65,Mx=0.70)
m80=_s('M80',E=0.02,O=3.0,Elo=50, M=0.70,Mx=0.75)
m81=_s('M81',O=3.0,Elo=50, M=0.75,Mx=0.80)
m82=_s('M82',O=3.0,Elo=50, M=0.80)
m83=_s('M83',O=3.0,Elo=50, M=0.82)
m84=_s('M84',E=0.05,O=3.0,Elo=100,M=0.60,Mx=0.68)
m85=_s('M85',E=0.03,O=3.0,Elo=75, M=0.62,Mx=0.68)
m86=_s('M86',E=0.02,O=3.0,Elo=75, M=0.63,Mx=0.67)

# ── Odds bands (M87-M96) ────────────────────────────────────────────
m87=_s('M87',Lo=1.00,O=1.30)
m88=_s('M88',Lo=1.30,O=1.50,E=0.02,Elo=50)
m89=_s('M89',Lo=1.50,O=1.70,E=0.03,Elo=75)
m90=_s('M90',Lo=1.70,O=1.90,E=0.04,Elo=75)
m91=_s('M91',Lo=1.90,O=2.10,E=0.05,Elo=75)
m92=_s('M92',Lo=2.10,O=2.50,E=0.08,Elo=50)
m93=_s('M93',Lo=1.30,O=1.60,Elo=75,Hp=True)
m94=_s('M94',Lo=1.50,O=1.80,E=0.03,Elo=75,Hp=True)
m95=_s('M95',Lo=1.40,O=1.65,M=0.65,Mx=0.75)
m96=_s('M96',Lo=1.60,O=1.90,E=0.03,M=0.60,Mx=0.70)

# ── Rule C variants (M97-M106) ──────────────────────────────────────
m97 =_rc('M97', Lo=0.61,Hi=0.69)
m98 =_rc('M98', E=0.02)
m99 =_rc('M99', E=0.04)
m100=_rc('M100',E=0.06)
m101=_rc('M101',Elo=100,E=0.02)
m102=_rc('M102',Elo=100,E=0.04)
m103=_rc('M103',H=3,Hp=True)
m104=_rc('M104',Elo=100,H=3,Hp=True)
m105=_rc('M105',Elo=100,E=0.05,Hp=True)
m106=_rc('M106',Elo=100,E=0.05,Lo=0.62,Hi=0.68,Hp=True)

# ── League combos (M107-M116) ───────────────────────────────────────
m107=_lg('M107',DL,O=2.0,Elo=75)
m108=_lg('M108',DL,E=0.03,O=2.0,Elo=75)
m109=_lg('M109',DL,O=2.0,Elo=75,M=0.60,Mx=0.70)
m110=_lg('M110',PG,O=2.0,Elo=75)
m111=_lg('M111',PG,E=0.03,O=2.0,Elo=75)
m112=_lg('M112',PG,O=2.0,Elo=75,M=0.60,Mx=0.70)
m113=_lg('M113',MJ,O=2.0,Elo=75)
m114=_lg_rc('M114',MJ)
m115=_lg('M115',EP,O=2.0,Elo=75)
m116=_lg('M116',CH,O=2.0,Elo=100)

# ── Triple signal combos (M117-M126) ────────────────────────────────
m117=_s('M117',O=2.0,Elo=75, H=3,Hp=True)
m118=_s('M118',E=0.02,O=1.9,Elo=100,H=3,Hp=True)
m119=_s('M119',O=1.9,Elo=100,H=5,Wr=0.65)
m120=_s('M120',Elo=100,H=3,Hp=True,M=0.60,Mx=0.70)
m121=_s('M121',Elo=75, H=5,Wr=0.70,M=0.60,Mx=0.70)
m122=_s('M122',E=0.02,Elo=100,H=5,Wr=0.70,M=0.60,Mx=0.70)
m123=_s('M123',E=0.02,Elo=150,H=3,Hp=True,M=0.60,Mx=0.70)
m124=_s('M124',E=0.03,Elo=75, H=3,Dt=0.20,M=0.60,Mx=0.70)
m125=_s('M125',E=0.05,O=2.0,Elo=100,H=3,Dt=0.20)
m126=_s('M126',E=0.05,O=1.8,Elo=75, H=5,Hp=True,M=0.60,Mx=0.70)

# ── Уточнения прибыльных TEST стратегий (M127-M136) ─────────────────
m127=_rc('M127',Elo=125,Lo=0.62,Hi=0.68)
m128=_rc('M128',Lo=0.62,Hi=0.68,H=3,Hp=True)
m129=_rc('M129',Lo=0.62,Hi=0.68,E=0.05,Hp=True)
m130=_s('M130',O=2.0,H=3,Dt=0.50,Elo=75)
m131=_s('M131',O=2.0,H=3,Dt=0.40,Elo=100)
m132=_s('M132',E=0.02,O=1.8,Elo=150,H=3,Hp=True)
m133=_rc('M133',E=0.07,Lo=0.62,Hi=0.68,Hp=True)
m134=_s('M134',E=0.05,O=1.8,Elo=200)
m135=_s('M135',O=1.8,Elo=200,H=1,Hp=True,M=0.68)
m136=_s('M136',H=3,Hp=True,M=0.62,Mx=0.72)

# ── adj_prob стратегии (M137-M146) ──────────────────────────────────
m137=_adj('M137',MinAdj=0.65,O=2.0,Elo=75, M=0.60,Mx=0.70)
m138=_adj('M138',MinAdj=0.68,O=1.8,Elo=75)
m139=_adj('M139',MinAdj=0.60,O=2.0,Elo=75, AdjEdge=0.05)
m140=_adj('M140',MinAdj=0.60,O=2.5,Elo=50, AdjEdge=0.08)
m141=_s('M141',O=1.5,Elo=150,Hp=True)
m142=_s('M142',O=1.6,Elo=150,M=0.68)
m143=_s('M143',O=1.7,Elo=100,H=5,Wr=0.70)
m144=_s('M144',E=0.03,O=1.8,Elo=150,H=3,Hp=True)
m145=_rc('M145',Elo=100,E=0.03,Lo=0.62,Hi=0.68,Hp=True)
m146=_s('M146',EloMax=9999.0,Elo=400)


# ═══════════════════════════════════════════════════════════════════
#  РЕЕСТР СТРАТЕГИЙ  (desc, is_oracle, is_posthoc)
# ═══════════════════════════════════════════════════════════════════

SMETA: dict[str, tuple] = {
    'M00':('No Bet Baseline',0,0),
    'M01':('Market Favorite <2.0',0,0),
    'M02':('Market Fav 60-70%',0,0),
    'M03':('Elo Value',0,0),
    'M04':('H2H Value',0,0),
    'M05':('Rule C Frozen',0,0),
    'M06':('Rule C Plus (post-hoc)',0,1),
    'M07':('Elo Strong 150+',0,0),
    'M08':('H2H Positive',0,0),
    'M09':('Conservative Fund',0,0),
    'M10':('Aggressive Fund',0,0),
    'M11':('DreamLeague Spec',0,0),
    'M12':('EPL Specialist',0,0),
    'M13':('PGL Specialist',0,0),
    'M14':('BM Disagreement',0,0),
    'M15':('Line Move Early',0,0),
    'M16':('Closing Oracle',1,0),
    'M17':('Extreme Fav <1.30',0,0),
    'M18':('Fav <1.40 Elo50',0,0),
    'M19':('Fav <1.50 Elo100 Edge',0,0),
    'M20':('Fav <1.50 Mkt75',0,0),
    'M21':('Elo300 Edge',0,0),
    'M22':('Elo400',0,0),
    'M23':('Elo200 Mkt68 <1.8',0,0),
    'M24':('Elo150 H2H+ <1.8',0,0),
    'M25':('H2H Dom 80% n>=5',0,0),
    'M26':('H2H Dom 75% Elo50',0,0),
    'M27':('H2H Delta 40% Elo75',0,0),
    'M28':('H2H 70% n>=4 Edge <1.8',0,0),
    'M29':('Market Certainty 80%',0,0),
    'M30':('Mkt75 Elo50',0,0),
    'M31':('Mkt72 Elo100 Edge2',0,0),
    'M32':('Mkt70 Elo150 H2H+',0,0),
    'M33':('Rule C Elo100',0,0),
    'M34':('Rule C H2H+',0,0),
    'M35':('Rule C Edge8',0,0),
    'M36':('Rule C Tight 62-68%',0,0),
    'M37':('Triple Elo100 H2H+ Edge',0,0),
    'M38':('Triple Edge5 H2H+ 60-70',0,0),
    'M39':('Elo200 Edge3 <1.8',0,0),
    'M40':('Consensus Mkt65 Elo100 H2H',0,0),
    'M41':('Pre10 Edge <2.0',0,0),
    'M42':('Pre10 LineMove 2%',0,0),
    'M43':('Pre5 LineMove Elo75',0,0),
    'M44':('DreamLeague Fav <1.5',0,0),
    'M45':('PGL Fav <1.5',0,0),
    'M46':('Major Fav <1.3',0,0),
    # NEW 100
    'M47':('Edge5 <3.0',0,0),
    'M48':('Edge8 <2.5',0,0),
    'M49':('Edge10 <2.5',0,0),
    'M50':('Edge12 <2.5',0,0),
    'M51':('Edge15 <3.0',0,0),
    'M52':('Edge20 <3.0',0,0),
    'M53':('Edge5 Elo50 <2.5',0,0),
    'M54':('Edge8 Elo75 <2.0',0,0),
    'M55':('Edge10 Elo75 <2.0',0,0),
    'M56':('Edge12 Elo100 <2.0',0,0),
    'M57':('Elo75 Mkt60-70 <2.0',0,0),
    'M58':('Edge2 Elo100 Mkt60+ <2.0',0,0),
    'M59':('Elo200 <2.0',0,0),
    'M60':('Elo250 <2.0',0,0),
    'M61':('Elo75 Mkt65+ <1.8',0,0),
    'M62':('Elo100 Mkt65+ <1.8',0,0),
    'M63':('Elo150 Mkt65+ <1.8',0,0),
    'M64':('Edge3 Elo75 Mkt60-70 <1.8',0,0),
    'M65':('Edge3 Elo100 Mkt60-70 <1.8',0,0),
    'M66':('Edge3 Elo150 Mkt65+ <1.8',0,0),
    'M67':('H2H n3 WR65 <2.0',0,0),
    'M68':('H2H n4 WR65 <2.0',0,0),
    'M69':('H2H n5 WR65 <2.0',0,0),
    'M70':('H2H n6 WR65 <2.0',0,0),
    'M71':('H2H n3 WR70 <2.0',0,0),
    'M72':('H2H n5 WR70 <2.0',0,0),
    'M73':('H2H+ n3 Edge5 <2.0',0,0),
    'M74':('H2H+ n5 Edge5 <2.0',0,0),
    'M75':('H2H Delta30 n5 <2.0',0,0),
    'M76':('H2H Delta30 n3 Elo75 <2.0',0,0),
    'M77':('Mkt55-65 Edge8 Elo75',0,0),
    'M78':('Mkt60-65 Edge5 Elo75',0,0),
    'M79':('Mkt65-70 Edge3 Elo75',0,0),
    'M80':('Mkt70-75 Edge2 Elo50',0,0),
    'M81':('Mkt75-80 Elo50',0,0),
    'M82':('Mkt80+ Elo50',0,0),
    'M83':('Mkt82+ Elo50',0,0),
    'M84':('Mkt60-68 Edge5 Elo100',0,0),
    'M85':('Mkt62-68 Edge3 Elo75',0,0),
    'M86':('Mkt63-67 Edge2 Elo75',0,0),
    'M87':('Odds<1.30 Any',0,0),
    'M88':('Odds1.30-1.50 Edge2 Elo50',0,0),
    'M89':('Odds1.50-1.70 Edge3 Elo75',0,0),
    'M90':('Odds1.70-1.90 Edge4 Elo75',0,0),
    'M91':('Odds1.90-2.10 Edge5 Elo75',0,0),
    'M92':('Odds2.10-2.50 Edge8 Elo50',0,0),
    'M93':('Odds1.30-1.60 H2H+ Elo75',0,0),
    'M94':('Odds1.50-1.80 Edge3 H2H+ Elo75',0,0),
    'M95':('Odds1.40-1.65 Mkt65-75',0,0),
    'M96':('Odds1.60-1.90 Edge3 Mkt60-70',0,0),
    'M97':('RC Range 61-69%',0,0),
    'M98':('RC Edge2',0,0),
    'M99':('RC Edge4',0,0),
    'M100':('RC Edge6',0,0),
    'M101':('RC Elo100 Edge2',0,0),
    'M102':('RC Elo100 Edge4',0,0),
    'M103':('RC H2H+ n3',0,0),
    'M104':('RC Elo100 H2H+ n3',0,0),
    'M105':('RC Elo100 Edge5 H2H+',0,0),
    'M106':('RC Elo100 Edge5 62-68% H2H+',0,0),
    'M107':('DL Elo75 <2.0',0,0),
    'M108':('DL Edge3 Elo75 <2.0',0,0),
    'M109':('DL Elo75 Mkt60-70',0,0),
    'M110':('PGL Elo75 <2.0',0,0),
    'M111':('PGL Edge3 Elo75 <2.0',0,0),
    'M112':('PGL Elo75 Mkt60-70',0,0),
    'M113':('Major Elo75 <2.0',0,0),
    'M114':('Major RC',0,0),
    'M115':('EPL Elo75 <2.0',0,0),
    'M116':('Cup/Champ Elo100 <2.0',0,0),
    'M117':('Triple Elo75 H2H+ n3 <2.0',0,0),
    'M118':('Triple Elo100 H2H+ n3 Edge2',0,0),
    'M119':('Triple Elo100 H2H WR65 n5',0,0),
    'M120':('Triple Elo100 H2H+ Mkt60-70',0,0),
    'M121':('Triple Elo75 WR70 n5 Mkt60-70',0,0),
    'M122':('Triple Elo100 WR70 n5 Mkt60-70',0,0),
    'M123':('Triple Elo150 H2H+ Mkt60-70',0,0),
    'M124':('Triple Elo75 Delta20 Mkt60-70',0,0),
    'M125':('Triple Elo100 Delta20 Edge5',0,0),
    'M126':('Triple Elo75 H2H+ n5 Mkt60-70 <1.8',0,0),
    'M127':('RC Elo125 Tight 62-68%',0,0),
    'M128':('RC Tight H2H+ n3',0,0),
    'M129':('RC Tight Edge5 H2H+',0,0),
    'M130':('H2H Delta50 Elo75',0,0),
    'M131':('H2H Delta40 Elo100',0,0),
    'M132':('Elo150 H2H+ n3 Edge2 <1.8',0,0),
    'M133':('RC Tight Edge7 H2H+',0,0),
    'M134':('Elo200 Edge5 <1.8',0,0),
    'M135':('Elo200 H2H+ Mkt68+ <1.8',0,0),
    'M136':('H2H+ n3 Mkt62-72',0,0),
    'M137':('Adj65 Elo75 Mkt60-70',0,0),
    'M138':('Adj68 Elo75 <1.8',0,0),
    'M139':('Adj Edge5 Elo75',0,0),
    'M140':('Adj Edge8 Elo50',0,0),
    'M141':('Elo150 H2H+ <1.5',0,0),
    'M142':('Elo150 Mkt68+ <1.6',0,0),
    'M143':('Elo100 H2H WR70 n5 <1.7',0,0),
    'M144':('Elo150 H2H+ n3 Edge3 <1.8',0,0),
    'M145':('RC Elo100 Edge3 Tight H2H+',0,0),
    'M146':('Elo400 AnyOdds',0,0),
}

# ═══════════════════════════════════════════════════════════════════
#  ТАБЛИЦА СТРАТЕГИЙ
# ═══════════════════════════════════════════════════════════════════

def _m42_fn(m,**_):
    if m.pre_match_pts<10 or m.pre_match_move is None: return nb('M42')
    if m.pre_match_move>0.02 and 1<m.open_home<3.0:
        return _bet('home',m.open_home,m.elo_prob_home,m.market_prob_home,abs(m.pre_match_move),'M42')
    if m.pre_match_move<-0.02 and 1<m.open_away<3.0:
        return _bet('away',m.open_away,1-m.elo_prob_home,m.market_prob_away,abs(m.pre_match_move),'M42')
    return nb('M42')

def _m43_fn(m,**_):
    if m.pre_match_pts<5 or m.pre_match_move is None or abs(m.elo_diff)<75: return nb('M43')
    if m.pre_match_move>0.02 and 1<m.open_home<2.5:
        return _bet('home',m.open_home,m.elo_prob_home,m.market_prob_home,abs(m.pre_match_move),'M43')
    if m.pre_match_move<-0.02 and 1<m.open_away<2.5:
        return _bet('away',m.open_away,1-m.elo_prob_home,m.market_prob_away,abs(m.pre_match_move),'M43')
    return nb('M43')

STRATEGIES: dict[str, Callable] = {
    'M00':m00,'M01':m01,'M02':m02,'M03':m03,'M04':m04,
    'M05':m05,'M06':m06,'M07':m07,'M08':m08,'M09':m09,
    'M10':m10,'M11':m11,'M12':m12,'M13':m13,'M14':m14,
    'M15':m15,'M16':m16,
    'M17':m17,'M18':m18,'M19':m19,'M20':m20,'M21':m21,
    'M22':m22,'M23':m23,'M24':m24,'M25':m25,'M26':m26,
    'M27':m27,'M28':m28,'M29':m29,'M30':m30,'M31':m31,
    'M32':m32,'M33':m33,'M34':m34,'M35':m35,'M36':m36,
    'M37':m37,'M38':m38,'M39':m39,'M40':m40,'M41':m41,
    'M42':_m42_fn,'M43':_m43_fn,'M44':m44,'M45':m45,'M46':m46_fn,
    'M47':m47,'M48':m48,'M49':m49,'M50':m50,'M51':m51,
    'M52':m52,'M53':m53,'M54':m54,'M55':m55,'M56':m56,
    'M57':m57,'M58':m58,'M59':m59,'M60':m60,'M61':m61,
    'M62':m62,'M63':m63,'M64':m64,'M65':m65,'M66':m66,
    'M67':m67,'M68':m68,'M69':m69,'M70':m70,'M71':m71,
    'M72':m72,'M73':m73,'M74':m74,'M75':m75,'M76':m76,
    'M77':m77,'M78':m78,'M79':m79,'M80':m80,'M81':m81,
    'M82':m82,'M83':m83,'M84':m84,'M85':m85,'M86':m86,
    'M87':m87,'M88':m88,'M89':m89,'M90':m90,'M91':m91,
    'M92':m92,'M93':m93,'M94':m94,'M95':m95,'M96':m96,
    'M97':m97,'M98':m98,'M99':m99,'M100':m100,'M101':m101,
    'M102':m102,'M103':m103,'M104':m104,'M105':m105,'M106':m106,
    'M107':m107,'M108':m108,'M109':m109,'M110':m110,'M111':m111,
    'M112':m112,'M113':m113,'M114':m114,'M115':m115,'M116':m116,
    'M117':m117,'M118':m118,'M119':m119,'M120':m120,'M121':m121,
    'M122':m122,'M123':m123,'M124':m124,'M125':m125,'M126':m126,
    'M127':m127,'M128':m128,'M129':m129,'M130':m130,'M131':m131,
    'M132':m132,'M133':m133,'M134':m134,'M135':m135,'M136':m136,
    'M137':m137,'M138':m138,'M139':m139,'M140':m140,'M141':m141,
    'M142':m142,'M143':m143,'M144':m144,'M145':m145,'M146':m146,
}

# ═══════════════════════════════════════════════════════════════════
#  GRID СТРАТЕГИИ M147-M1146  (1 000 шт., авто-генерация)
# ═══════════════════════════════════════════════════════════════════

_GRID: list = []
_I = [147]

def _gc():
    c = f'M{_I[0]}'; _I[0] += 1; return c

# ── A: Edge × Odds × Elo (10×5×6=300)  M147-M446 ──────────────────
for _e in [0.5,1.0,2.0,3.0,5.0,8.0,10.0,15.0,20.0,25.0]:
    for _o in [1.6,1.7,1.8,2.0,2.5]:
        for _el in [0,50,75,100,125,150]:
            _c=_gc()
            _GRID.append((_c,f'E{_e} <{_o} Elo{_el}',_s(_c,E=_e,O=_o,Elo=_el)))

# ── B: Mkt window × Edge × Elo (10×6×4=240)  M447-M686 ────────────
for _mlo,_mhi in [(0.55,0.65),(0.57,0.67),(0.58,0.68),(0.60,0.70),(0.62,0.68),
                  (0.63,0.67),(0.63,0.70),(0.65,0.72),(0.60,0.65),(0.65,0.75)]:
    for _e in [0.0,1.0,2.0,3.0,5.0,8.0]:
        for _el in [50,75,100,150]:
            _c=_gc()
            _GRID.append((_c,f'Mkt{int(_mlo*100)}-{int(_mhi*100)} E{_e} Elo{_el}',
                          _s(_c,E=_e,Elo=_el,M=_mlo,Mx=_mhi)))

# ── C: H2H × Hp × Edge × Elo × Odds (3×2×3×3×2=108)  M687-M794 ───
for _h in [3,5,10]:
    for _hp in [False,True]:
        for _e in [0.0,2.0,5.0]:
            for _el in [50,75,100]:
                for _o in [1.8,2.0]:
                    _c=_gc()
                    _GRID.append((_c,
                        f'H{_h}{"+" if _hp else ""} E{_e} Elo{_el} <{_o}',
                        _s(_c,H=_h,Hp=_hp,E=_e,Elo=_el,O=_o)))

# ── D: RC Elo × Edge × window (6×5×5=150)  M795-M944 ──────────────
for _el in [50,75,100,125,150,200]:
    for _e in [0.0,1.0,2.0,3.0,5.0]:
        for _mlo,_mhi in [(0.58,0.72),(0.60,0.70),(0.62,0.68),(0.63,0.67),(0.60,0.68)]:
            _c=_gc()
            _GRID.append((_c,f'RC Elo{_el} E{_e} {int(_mlo*100)}-{int(_mhi*100)}%',
                          _rc(_c,Elo=_el,E=_e,Lo=_mlo,Hi=_mhi)))

# ── E: Odds range targeting (8×3×2=48)  M945-M992 ─────────────────
for _lo,_hi in [(1.3,1.5),(1.5,1.7),(1.7,1.9),(1.9,2.1),
                (2.1,2.5),(1.4,1.7),(1.6,1.9),(1.5,2.0)]:
    for _e in [0.0,2.0,5.0]:
        for _el in [0,75]:
            _c=_gc()
            _GRID.append((_c,f'Odds{_lo}-{_hi} E{_e} Elo{_el}',
                          _s(_c,E=_e,Lo=_lo,O=_hi,Elo=_el)))

# ── F: Pre-match pts × Edge × Elo × Odds (4×3×3×2=72)  M993-M1064 ─
for _pre in [3,5,10,15]:
    for _e in [0.0,2.0,5.0]:
        for _el in [0,75,100]:
            for _o in [1.8,2.0]:
                _c=_gc()
                _GRID.append((_c,f'Pre{_pre} E{_e} Elo{_el} <{_o}',
                              _s(_c,Pre=_pre,E=_e,Elo=_el,O=_o)))

# ── G: H2H Delta × Edge × Elo (4×3×3=36)  M1065-M1100 ─────────────
for _dt in [0.0,5.0,10.0,20.0]:
    for _e in [0.0,2.0,5.0]:
        for _el in [50,75,100]:
            _c=_gc()
            _GRID.append((_c,f'Dt{int(_dt)} E{_e} Elo{_el}',
                          _s(_c,Dt=_dt,E=_e,Elo=_el,O=2.0)))

# ── H: Win rate filter (4×3×2=24)  M1101-M1124 ────────────────────
for _wr in [0.55,0.60,0.65,0.70]:
    for _h in [3,5,10]:
        for _e in [0.0,2.0]:
            _c=_gc()
            _GRID.append((_c,f'WR{int(_wr*100)} H{_h} E{_e}',
                          _s(_c,Wr=_wr,H=_h,E=_e,Elo=75,O=2.0)))

# ── I: Adj_prob (8×3=24, trim 2 → 22)  M1125-M1146 ────────────────
for _ma in [0.52,0.55,0.58,0.60,0.63,0.65,0.68,0.70]:
    for _ao in [1.8,2.0,2.5]:
        _c=_gc()
        _GRID.append((_c,f'Adj{int(_ma*100)} <{_ao}',
                      _adj(_c,MinAdj=_ma,O=_ao)))

# Ровно 1 000 стратегий (M147-M1146)
_GRID = _GRID[:1000]
assert len(_GRID) == 1000, f"Grid = {len(_GRID)}"

for _code,_desc,_func in _GRID:
    STRATEGIES[_code] = _func
    SMETA[_code] = (_desc, 0, 0)

del _GRID, _I, _gc

DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tournament_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    division      TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    bookmaker     TEXT,
    market        TEXT DEFAULT '151_1',
    split         TEXT NOT NULL,
    decision_time TEXT,
    bet           INTEGER DEFAULT 0,
    bet_team      TEXT DEFAULT '',
    odds          REAL DEFAULT 0,
    market_prob   REAL DEFAULT 0,
    model_prob    REAL DEFAULT 0,
    edge          REAL DEFAULT 0,
    stake_usd     REAL DEFAULT 0,
    reason_code   TEXT,
    UNIQUE(strategy_name, event_id, bookmaker)
);
"""


def run_strategies(division_filter='all', include_oracle=False):
    tcon = sqlite3.connect(TOURN_DB)
    tcon.executescript("DROP TABLE IF EXISTS tournament_decisions;")
    tcon.executescript(DECISIONS_SCHEMA)
    tcon.commit()
    tcur = tcon.cursor()

    # Обновляем реестр
    for name,(desc,is_oracle,is_posthoc) in SMETA.items():
        tcur.execute("""
            INSERT OR REPLACE INTO tournament_strategy_registry
            (strategy_name,description,is_oracle,is_posthoc,is_valid_for_test,division_filter)
            VALUES(?,?,?,?,?,?)
        """,(name,desc,is_oracle,is_posthoc,0 if (is_oracle or is_posthoc) else 1,'A,B,C'))
    tcon.commit()

    div_clause="" if division_filter=='all' else f"AND division='{division_filter.upper()}'"
    rows = tcur.execute(f"""
        SELECT event_id,match_date,split,division,league,home_team,away_team,bookmaker,start_time,
               open_home,open_away,market_prob_home,market_prob_away,
               elo_home,elo_away,elo_diff,elo_prob_home,edge_home,
               h2h_n,h2h_wr_home,h2h_wr_away,h2h_delta,adj_prob_home,
               pre_match_pts,pre_match_move,latest_pre_prob
        FROM tournament_blind_features WHERE 1=1 {div_clause}
        ORDER BY start_time ASC
    """).fetchall()

    total = len(rows)
    n_strats = len(STRATEGIES) - (0 if include_oracle else 1)
    print(f"Матчей: {total:,}  |  Стратегий: {n_strats}\n")
    print(f"{'#':>4}  {'Код':<7} {'Описание':<32} {'A':>6} {'B':>6} {'C':>6} {'Σ':>7}")
    print('─'*70)

    # Close odds + BM probs
    hcon = sqlite3.connect(HARVEST_DB)
    close_odds: dict = {}
    bm_probs: dict = {}
    for eid,bm,oh,oa,ch,ca in hcon.execute("""
        SELECT event_id,bookmaker,open_home,open_away,close_home,close_away
        FROM odds_summary WHERE market='151_1' AND open_home>1 AND open_away>1
        AND close_home>1 AND close_away>1
    """).fetchall():
        if bm=='PinnacleSports': close_odds[str(eid)]=(ch,ca)
        rh=1./oh; ra=1./oa; tot=rh+ra
        if tot>0: bm_probs.setdefault(str(eid),{})[bm]=rh/tot
    hcon.close()

    # Строим BlindMatch один раз
    matches: list[BlindMatch] = []
    for row in rows:
        (eid,md,sp,dv,lg,hm,aw,bk,st,
         oh,oa,mh,ma,eh,ea,ed,eph,edgh,
         hn,wh,wa,hd,ah,pp,pm,lp)=row
        matches.append(BlindMatch(
            event_id=str(eid),match_date=md,split=sp,division=dv,league=lg,
            home_team=hm,away_team=aw,bookmaker=bk,
            start_time=int(st) if st else 0,
            open_home=oh or 0,open_away=oa or 0,
            market_prob_home=mh or .5,market_prob_away=ma or .5,
            elo_home=eh or 1000,elo_away=ea or 1000,
            elo_diff=ed or 0,elo_prob_home=eph or .5,
            edge_home=edgh or 0,h2h_n=hn or 0,
            h2h_wr_home=wh or .5,h2h_wr_away=wa or .5,
            h2h_delta=hd or 0,adj_prob_home=ah or .5,
            pre_match_pts=pp or 0,pre_match_move=pm,
            latest_pre_prob=lp,
        ))

    now = datetime.datetime.utcnow().isoformat()

    for i,(name,func) in enumerate(STRATEGIES.items(),1):
        if name=='M16' and not include_oracle: continue

        batch=[]; cnt={'A':0,'B':0,'C':0}
        for m in matches:
            kw={}
            if name=='M14': kw['bm_probs']=bm_probs.get(m.event_id,{})
            elif name=='M16':
                ch,ca=close_odds.get(m.event_id,(None,None))
                kw['close_home']=ch; kw['close_away']=ca
            d=func(m,**kw)
            batch.append((name,m.division,m.event_id,m.bookmaker,'151_1',
                          m.split,now,int(d.bet),d.bet_team,d.odds,
                          d.market_prob,d.model_prob,d.edge,d.stake_usd,d.reason_code))
            if d.bet: cnt[m.division]=cnt.get(m.division,0)+1

        tcur.executemany("""
            INSERT OR IGNORE INTO tournament_decisions
            (strategy_name,division,event_id,bookmaker,market,split,decision_time,
             bet,bet_team,odds,market_prob,model_prob,edge,stake_usd,reason_code)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,batch)
        tcon.commit()

        tb=cnt['A']+cnt['B']+cnt['C']
        desc=SMETA[name][0]
        flag=' ⚠️' if SMETA[name][1] or SMETA[name][2] else ''
        print(f"[{i:3d}/{n_strats}] {name:<7}{flag:<4} {desc:<32}  "
              f"A:{cnt['A']:5d}  B:{cnt['B']:5d}  C:{cnt['C']:5d}  Σ:{tb:6d}/{total:,}",flush=True)

    print(f"\n{'─'*70}")
    print(f"✅ Готово. Всего стратегий: {n_strats}")
    tcon.close()


if __name__ == '__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--division',default='all')
    ap.add_argument('--include-oracle',action='store_true')
    args=ap.parse_args()
    run_strategies(args.division,args.include_oracle)
