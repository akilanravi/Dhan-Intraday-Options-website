"""Process-shared dashboard scanner and paper writer, independent of tab widgets."""
from types import SimpleNamespace
from threading import RLock
from queue import SimpleQueue, Empty
import time
import pandas as pd
import streamlit as st
from paper_engine import PaperStore, refresh_cycle, worker_pool
from worker_store import cloud_journal
from scan_clock import scheduled_slot, regular_day

@st.cache_resource
def runtimes():
    return {}, RLock()

def get_runtime(key, mode, token, gist, owner):
    items, lock = runtimes()
    with lock:
        if key not in items:
            items[key]=Runtime(mode,token,gist,owner)
        return items[key]

class Runtime:
    def __init__(self, mode, github, gist, owner):
        self.lock=RLock()
        self.mode=mode
        self.github=github
        self.owner=owner
        self.store=PaperStore(github,gist)
        self.journal=pd.DataFrame()
        self.rows=[]
        self.candles={}
        self.notes=[]
        self.stamp=''
        self.slot=None
        self.requested_slot=None
        self.auto_slot=None
        self.progress=None
        self.preview_rows={}
        self.preview_candles={}
        self.complete_coverage=False
        self.notice=''
        self.manual_requested=False
        self.queue=SimpleQueue()
        self.future=None
        self.load_future=None
        self.cloud=None
        self.loaded=False
        self.dirty=False
        self.error=''
        self.checked=-1e6
        self.cloud_status={}

    def load(self, previous_id):
        if self.mode:
            if self.cloud is None:
                self.cloud=cloud_journal(self.github,self.store.gist,'intraday')
            return self.cloud.read(snapshot=True,previous_id=previous_id)
        return self.store.load(),None,None

    def finish_load(self):
        try:
            frame,status,snap=self.load_future.result()
            if frame is not None:self.journal=frame
            if self.mode and status is not None:
                if snap is not None:
                    self.rows,self.candles,slot=snap
                    self.slot=pd.Timestamp(slot) if slot else None
                self.cloud_status=status
                self.notes=status.get('notes',[])
                self.stamp=status.get('checked_at','')
            self.loaded=True
            self.error=''
        except Exception as e:
            self.error=f'Paper journal / worker data unavailable: {str(e)[:180]}'
        finally:
            self.load_future=None

    def step(self, clock, settings, request, lookup, token, client, params, owner, priority=()):
        with self.lock:
            # Any connected viewer can drive scheduling. A single lock and future
            # still serialize journal writes. The controller only owns policy.
            if (request and not self.mode) or time.monotonic()-getattr(self,'owner_seen',-1e6)>90:
                self.owner=owner
            controller=self.owner==owner
            if controller:
                self.owner_seen=time.monotonic()
                self.current_inputs=(token,client,params.copy(),tuple(priority))
            if request and not self.mode:
                if self.future is not None:
                    self.notice='Scan already running. Results appear as symbols finish.'
                else:
                    self.manual_requested=True
                    self.notice='Refresh requested.'
            if self.load_future is not None and self.load_future.done():self.finish_load()
            if self.load_future is None and self.future is None and (self.mode or not self.loaded) and (request or time.monotonic()-self.checked>=15):
                self.checked=time.monotonic()
                self.load_future=worker_pool().submit(self.load,self.cloud_status.get('snapshot_id',''))
            while True:
                try:packet=self.queue.get_nowait()
                except Empty:break
                self.progress=packet[:4]
                if len(packet)>4:
                    rows,candles=packet[4:]
                    if rows:
                        symbol=packet[2]
                        self.preview_rows[symbol]=rows
                        self.preview_candles[symbol]=candles
            if self.future is not None and self.future.done():
                try:
                    rows,candles,journal,self.notes,_,self.dirty,self.stamp=self.future.result()
                    self.complete_coverage=len({r['symbol'] for r in rows})==len(lookup) and bool(rows)
                    if rows or not self.rows:
                        self.rows,self.candles,self.slot=rows,candles,self.requested_slot
                    else:
                        self.notes.insert(0,'No new rows published. Showing the previous snapshot.')
                    if self.loaded:self.journal=journal
                except Exception as e:
                    self.notes=['Refresh failed: '+str(e)[:180]]
                self.future=None
                self.notice=''
            slot=scheduled_slot(clock,settings)
            grace=regular_day(clock,settings) and (9,20)<=(clock.hour,clock.minute)<(15,35) and slot is None
            if not self.mode and self.future is None and self.load_future is None and not grace:
                manual=self.manual_requested
                self.manual_requested=False
                if manual and not self.dirty and slot is not None and slot==self.slot and self.complete_coverage:
                    manual=False
                    self.notice='All symbols are already up to date for this candle.'
                if (slot is not None and slot!=self.auto_slot) or manual:
                    historical=slot is None
                    self.requested_slot=slot
                    if slot is not None:self.auto_slot=slot
                    self.progress=(0,0,'','')
                    self.preview_rows={}
                    self.preview_candles={}
                    self.queue=SimpleQueue()
                    self.notice='Scan started. Results will appear as they arrive.'
                    t,c,policy,first=getattr(self,'current_inputs',(token,client,params,priority))
                    ordered=list(dict.fromkeys([*(s for s in first if s in lookup),*lookup]))
                    self.policy=policy.copy()
                    allowed=slot is not None and not historical
                    self.future=worker_pool().submit(refresh_cycle,t,c,lookup,ordered,self.journal,
                        **policy,store=self.store,dirty=self.dirty,progress=self.queue,journal_ready=self.loaded,
                        slot=slot,allow_trades=allowed)
            partial=self.future is not None and bool(self.preview_rows)
            rows=[r for pair in self.preview_rows.values() for r in pair] if partial else self.rows
            candles=self.preview_candles.copy() if partial else self.candles
            return SimpleNamespace(rows=rows,candles=candles,journal=self.journal,notes=self.notes,
                partial=partial,preview_slot=self.requested_slot if partial else None,notice=self.notice,
                stamp=self.stamp,slot=self.slot,progress=self.progress,future=self.future,error=self.error,
                status=self.cloud_status,gist=self.store.gist,controller=controller,
                policy=self.cloud_status.get('policy',{}) if self.mode else getattr(self,'policy',params))
