// Presentation-only telemetry derived from Board's existing project and timeline data.
function tacticalConsole() {
  return {
    connectionState: 'connecting',
    lastSyncAt: null,
    effectsEnabled: true,
    systemReducedMotion: false,

    initTacticalConsole() {
      this._motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
      this.systemReducedMotion = this._motionQuery.matches;
      try { this.effectsEnabled = localStorage.getItem('board.visualEffects') !== 'off'; } catch {}
      this._motionChange = event => {
        this.systemReducedMotion = event.matches;
        this.applyVisualEffects();
      };
      this._motionQuery.addEventListener('change', this._motionChange);
      this.applyVisualEffects();
    },

    visualEffectsOn() { return this.effectsEnabled && !this.systemReducedMotion; },

    toggleVisualEffects() {
      this.effectsEnabled = !this.effectsEnabled;
      try { localStorage.setItem('board.visualEffects', this.effectsEnabled ? 'on' : 'off'); } catch {}
      this.applyVisualEffects();
    },

    applyVisualEffects() {
      document.body.dataset.motion = this.visualEffectsOn() ? 'on' : 'off';
      this.syncGraphMotion();
    },

    syncGraphMotion() {
      if (!this.cy) return;
      if (!this.activityMotionOn()) {
        this.cy.elements().stop(true, false);
        this.cy.elements().removeStyle('opacity');
        this.cy.elements().removeScratch('_pulseActive');
        this.cy.elements().removeScratch('_flowActive');
        this.cy.nodes().removeStyle('background-opacity');
        this.cy.edges().removeStyle('line-dash-offset');
      } else {
        this.pulseActive();
      }
    },

    activityMotionOn() {
      return this.visualEffectsOn() && this.project?.project?.status === 'active'
        && (this.replay.active ? this.replay.playing : this.connectionState === 'online');
    },

    markSyncSuccess() {
      this.connectionState = 'online';
      this.lastSyncAt = new Date().toISOString();
    },

    markSyncFailure() {
      this.connectionState = 'offline';
      this.syncGraphMotion();
    },

    connectionLabel() {
      if (this.replay.active) return 'REPLAY / HISTORICAL';
      return { connecting: 'CONNECTING', online: 'API CONNECTED', offline: 'CONNECTION LOST' }[this.connectionState];
    },

    portfolioTotal(field) { return this.projects.reduce((total, p) => total + (Number(p[field]) || 0), 0); },

    operationCounts() {
      const intents = this.project?.intents || [];
      return {
        facts: (this.project?.facts || []).filter(f => !['origin', 'goal'].includes(f.id)).length,
        running: intents.filter(i => !i.excluded && !i.to && i.worker).length,
        queued: intents.filter(i => !i.excluded && !i.to && !i.worker && !i.dispatch_error && !i.cancel_requested_at).length,
        concluded: intents.filter(i => i.to).length,
      };
    },

    activityLabel() {
      if (this.replay.active) return this.replay.playing ? 'REPLAY RUNNING' : 'REPLAY PAUSED';
      if (this.connectionState === 'offline') return 'DATA STALE';
      const status = this.project?.project?.status;
      if (status !== 'active') return { stopped: 'OPERATION SUSPENDED', completed: 'OPERATION COMPLETE', aborted: 'OPERATION ABORTED' }[status] || 'STANDBY';
      return this.project?.project?.reason || this.operationCounts().running > 0 ? 'AGENTS WORKING' : 'AWAITING ACTIVITY';
    },

    recentTelemetry() { return this.timelineEvents().slice(-4).reverse(); },
  };
}
