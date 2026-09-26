// Live report findings. Reads the same structured records as the human report.
function liveFindingsView() {
  const severityRank = { critical: 0, high: 1, medium: 2, low: 3, info: 4, negative: 5 };
  const signature = items => JSON.stringify(items);
  return {
    liveProjectId: '',
    liveFindings: [],
    liveLoaded: false,
    liveLoading: false,
    liveError: '',
    liveSyncedAt: null,
    liveTab: 'findings',
    liveFilter: 'all',
    liveSort: 'recent',
    liveExpanded: false,
    liveFollow: true,
    livePending: null,
    liveNewIds: [],
    liveOpenIds: [],
    liveSyncSequence: 0,
    _liveRequest: 0,
    _liveFlashTimer: null,

    resetLiveFindings(projectId = '') {
      if (projectId === this.liveProjectId) return;
      this._liveRequest++;
      clearTimeout(this._liveFlashTimer);
      this.liveProjectId = projectId;
      this.liveFindings = [];
      this.livePending = null;
      this.liveLoaded = false;
      this.liveLoading = false;
      this.liveError = '';
      this.liveSyncedAt = null;
      this.liveNewIds = [];
      this.liveOpenIds = [];
      this.liveFollow = true;
      this.liveFilter = 'all';
      this.liveTab = 'findings';
    },

    async loadLiveFindings(projectId) {
      if (this.replay.active || this.selectedProjectId !== projectId) return;
      this.resetLiveFindings(projectId);
      const request = ++this._liveRequest;
      this.liveLoading = true;
      try {
        const findings = await this.api('GET', `/projects/${encodeURIComponent(projectId)}/findings`);
        if (request !== this._liveRequest || projectId !== this.selectedProjectId || this.replay.active) return;
        if (!Array.isArray(findings)) throw new Error('Unexpected findings response');
        this.liveError = '';
        this.liveSyncedAt = new Date().toISOString();
        this.liveSyncSequence++;
        if (!this.liveFollow && this.liveLoaded) {
          this.livePending = signature(findings) === signature(this.liveFindings) ? null : findings;
        } else {
          this.applyLiveFindings(findings);
        }
      } catch (error) {
        if (request === this._liveRequest && projectId === this.selectedProjectId && !this.replay.active) {
          this.liveError = 'Findings could not be refreshed. Displaying the last received data.';
        }
      } finally {
        if (request === this._liveRequest) this.liveLoading = false;
      }
    },

    applyLiveFindings(findings) {
      const previous = new Map(this.liveFindings.map(f => [f.id, signature(f)]));
      const changed = this.liveLoaded ? findings.filter(f => previous.get(f.id) !== signature(f)).map(f => f.id) : [];
      if (signature(findings) !== signature(this.liveFindings)) {
        this.liveFindings = findings;
        this.liveNewIds = changed;
        clearTimeout(this._liveFlashTimer);
        this._liveFlashTimer = setTimeout(() => { this.liveNewIds = []; }, 4000);
        if (this.liveFollow) this.$nextTick(() => {
          if (this.$refs.findingScroll) this.$refs.findingScroll.scrollTop = 0;
        });
      }
      this.liveLoaded = true;
      this.livePending = null;
    },

    livePendingCount() {
      if (!this.livePending) return 0;
      const previous = new Map(this.liveFindings.map(f => [f.id, signature(f)]));
      const next = new Set(this.livePending.map(f => f.id));
      return this.livePending.filter(f => previous.get(f.id) !== signature(f)).length
        + this.liveFindings.filter(f => !next.has(f.id)).length;
    },

    followLiveFindings() {
      this.liveFollow = true;
      if (this.livePending) this.applyLiveFindings(this.livePending);
      this.$nextTick(() => { if (this.$refs.findingScroll) this.$refs.findingScroll.scrollTop = 0; });
    },

    toggleLiveFollow() {
      if (this.liveFollow) this.liveFollow = false;
      else this.followLiveFindings();
    },

    onFindingsScroll(event) {
      if (event.target.scrollTop > 40) this.liveFollow = false;
    },

    visibleLiveFindings() {
      if (this.replay.active) return [];
      const findings = this.liveFindings.filter(f => this.liveFilter === 'all'
        || (this.liveFilter === 'priority' ? ['critical', 'high'].includes(f.severity) : f.severity === this.liveFilter));
      return [...findings].sort((a, b) => {
        const severity = (severityRank[a.severity] ?? 4) - (severityRank[b.severity] ?? 4);
        const recent = (b.updated_at || b.created_at || '').localeCompare(a.updated_at || a.created_at || '');
        return (this.liveSort === 'priority' ? severity || recent : recent || severity) || a.id.localeCompare(b.id);
      });
    },

    findingSeverityCount(severities) { return this.liveFindings.filter(f => severities.includes(f.severity)).length; },
    findingSeverity(severity) { return severity === 'negative' ? 'Coverage' : (severity || 'info'); },
    findingIsOpen(id) { return this.liveOpenIds.includes(id); },
    toggleFinding(id) {
      this.liveFollow = false;
      this.liveOpenIds = this.findingIsOpen(id) ? this.liveOpenIds.filter(key => key !== id) : [...this.liveOpenIds, id];
    },

    findingFactAvailable(finding) {
      return !this.replay.active && finding.fact_id !== 'goal' && !!this.project?.facts.some(f => f.id === finding.fact_id);
    },

    inspectFinding(finding) {
      if (!this.findingFactAvailable(finding)) return;
      this.selectedFacts = [finding.fact_id];
      this.selectFact(finding.fact_id);
      this.sideTab = 'detail';
      this.centerGraphOnFact(finding.fact_id);
    },

    findingIntentHelp(finding) {
      if (!this.findingFactAvailable(finding)) return 'The source fact is unavailable in this view';
      if (this.projectIsActive()) return 'Draft an intent linked to this finding’s source fact';
      return { completed: 'Reopen the project to create an intent', stopped: 'Resume the project to create an intent', aborted: 'This project was aborted' }[this.project?.project?.status] || 'Project is not active';
    },

    createIntentFromFinding(finding) {
      if (!this.projectIsActive() || !this.findingFactAvailable(finding)) return;
      this.inspectFinding(finding);
      this.openCreateIntent();
      this.intentForm.description = `Follow up on ${finding.id}: ${finding.title}`
        + (finding.affected_target ? `\n\nTarget: ${finding.affected_target}` : '')
        + (finding.recommendation ? `\n\nSuggested next step:\n${finding.recommendation}` : '\n\nValidate this finding and record the supporting evidence.');
    },

    liveViewStatus() {
      if (this.replay.active) return 'HISTORICAL REPLAY';
      if (this.liveError) return 'FINDINGS STALE';
      if (this.connectionState === 'offline') return 'CONNECTION LOST';
      if (!this.liveLoaded) return 'CONNECTING';
      return this.project?.project?.status === 'active' ? 'LIVE / AUTO SYNC' : 'PROJECT ' + (this.project?.project?.status || '').toUpperCase();
    },

    liveOperations() {
      if (!this.project || this.project.project.status !== 'active') return [];
      const rows = [];
      const reason = this.project.project.reason;
      if (reason) rows.push({ id: 'reason', kind: 'ORIENT', worker: reason.worker, text: 'Reasoning over the fact graph', heartbeat: reason.last_heartbeat_at });
      for (const intent of this.project.intents) {
        if (!intent.to && intent.worker) rows.push({ id: 'intent-' + intent.id, kind: 'ACT / ' + intent.id, worker: intent.worker, text: intent.description, heartbeat: intent.last_heartbeat_at });
      }
      for (const job of this.project.jobs || []) {
        if (job.status === 'running') rows.push({ id: 'job-' + job.id, kind: 'JOB / ' + job.id, worker: job.creator || 'worker', text: job.description, heartbeat: job.last_check_at || job.created_at });
      }
      return rows;
    },

    liveActivityEvents() { return [...this.timelineEvents()].reverse(); },
  };
}
