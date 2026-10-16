function suspensionControls() {
  return {
    showResumeModal: false,
    resumeBusy: false,
    resumeError: '',
    resumeForm: { id: '', title: '', notes: '', excluded: [], confirmedJobs: [], intents: [], jobs: [], waiting: false },
    suspensionLabel(meta) {
      if (meta?.status === 'completed' && meta.completion_outcome === 'current_results') return 'concluded';
      if (meta?.status === 'active' && meta.planner_wait && !meta.reason) return 'planner waiting';
      if (meta?.status !== 'stopped') return meta?.status || '';
      return meta.suspension_id && meta.suspension_ack_id !== meta.suspension_id ? 'suspending' : 'suspended';
    },
    suspensionMessage() {
      const meta = this.project?.project;
      if (!meta || meta.status !== 'stopped') return '';
      const jobs = (this.project.jobs || []).filter(j => j.stop_state && j.stop_state !== 'stopped');
      const state = this.suspensionLabel(meta);
      if (state === 'suspending') return 'Suspending — waiting for the dispatcher to stop workers. Progress is preserved.';
      if (jobs.length) return `Workers suspended. ${jobs.length} background job(s) still need cleanup; inspect them in Resume.`;
      return 'Suspended — progress is preserved. This project stays paused until you click Resume.';
    },
    async openResumeProject(id) {
      try {
        const detail = await this.api('GET', `/projects/${id}`);
        if (detail.project.status !== 'stopped') throw new Error('This project is no longer suspended.');
        this.resumeForm = {
          id, title: detail.project.title, notes: detail.project.execution_notes || '',
          excluded: detail.intents.filter(i => i.excluded).map(i => i.id), confirmedJobs: [],
          intents: detail.intents.filter(i => !i.to), jobs: detail.jobs || [],
          waiting: this.suspensionLabel(detail.project) === 'suspending',
        };
        this.resumeError = '';
        this.showResumeModal = true;
        this.$nextTick(() => document.getElementById('resume-notes')?.focus());
      } catch (e) { this.showToast(e.message, 'error'); }
    },
    syncResumeState(detail) {
      if (!this.showResumeModal || detail.project.id !== this.resumeForm.id) return;
      this.resumeForm.waiting = this.suspensionLabel(detail.project) === 'suspending';
      this.resumeForm.jobs = detail.jobs || [];
    },
    resumeCleanupJobs() { return this.resumeForm.jobs.filter(j => j.stop_state && j.stop_state !== 'stopped'); },
    resumeBlocked() {
      return this.resumeBusy || this.resumeForm.waiting || this.resumeCleanupJobs().some(j =>
        j.stop_state === 'pending' || !this.resumeForm.confirmedJobs.includes(j.id));
    },
    async submitResume() {
      if (this.resumeBlocked()) return;
      this.resumeBusy = true;
      this.resumeError = '';
      const id = this.resumeForm.id;
      try {
        await this.api('PUT', `/projects/${id}/status`, {
          status: 'active', execution_notes: this.resumeForm.notes,
          excluded_intent_ids: this.resumeForm.excluded,
          confirmed_stopped_job_ids: this.resumeForm.confirmedJobs,
        });
        this.showResumeModal = false;
        await this.loadProjects();
        if (this.selectedProjectId === id) {
          await this.loadProject(id);
          this.updateGraph();
        }
        this.showToast('Project resumed');
      } catch (e) {
        this.resumeError = e.message;
        try { this.syncResumeState(await this.api('GET', `/projects/${id}`)); } catch {}
      } finally { this.resumeBusy = false; }
    },
  };
}
