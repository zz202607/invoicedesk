/* InvoiceDesk workbench. Runs inside pywebview; all data comes from app/api.py. */
const { createApp } = Vue;

createApp({
  data() {
    return {
      boot: {
        version: '',
        today: '',
        default_month: '',
        categories: [],
        default_category: '其他费用',
        mileage_rate: '1.5',
        layouts: [],
        batches: [],
        report_choices: { company: [], department: [], reporter: [] },
        recent_import_paths: [],
        unrecognized_count: 0,
      },
      loading: true,
      fatal: '',
      filters: {
        batch_id: '',
        search: '',
        unlinked_only: false,
        unrecognized_only: false,
        include_review: true,
        date_from: '',
        date_to: '',
      },
      unrecognizedDismissed: false,
      invoices: [],
      invoiceSummary: {
        count: 0,
        total: '0.00',
        review_count: 0,
        linked_count: 0,
        missing_amount_count: 0,
      },
      invoicesLoading: false,
      selected: [],
      reports: [],
      currentReportId: '',
      report: null,
      lines: [],
      activeSequence: null,
      preview: null,
      previewZoom: 1.25,
      previewForm: null,
      invoiceEdits: [],
      previewSaving: false,
      reviewQueue: null,
      reviewConfirmed: [],
      reviewResume: false,
      reviewSaving: false,
      fieldLabels: {
        invoice_number: '票号',
        invoice_date: '日期',
        seller_name: '销售方',
        buyer_name: '购买方',
        amount_total_cents: '金额',
        category: '分类',
        item_summary: '项目',
        category_rule: '分类来源',
        category_refresh: '分类（规则更新）',
        review: '复核',
        warning: '警告',
        archive: '归档',
        conflict: '重复票',
      },
      poolVisible: true,
      editingCategoryId: null,
      categoryDraft: '',
      categorySaving: false,
      quickProject: '',
      layout: 'project',
      showReportModal: false,
      reportModalMode: 'create',
      reportForm: {
        company: '',
        reporter: '',
        month: '',
        department: '',
        title: '',
        note: '',
      },
      showMileageModal: false,
      mileageForm: {
        line_date: '',
        km: '',
        description: '',
        category: '汽车费用',
        project_name: '',
      },
      showImportModal: false,
      importForm: { path: '', label: '' },
      importPlan: null,
      importProgress: {
        active: false,
        stage: '',
        done: 0,
        total: 0,
        name: '',
        cancelling: false,
      },
      pickingFolder: false,
      showWizard: false,
      wizardStep: 1,
      wizardBusy: false,
      pickingWizardFolder: false,
      wizardStats: {
        loading: false,
        count: 0,
        total: '0.00',
        review_count: 0,
        missing_amount_count: 0,
        categories: [],
        unlinked_ids: [],
      },
      wizard: {
        month: '',
        folder: '',
        label: '',
        importResult: null,
        batchId: null,
        reportId: '',
        reportSummary: '',
        addedCount: 0,
        skipped: null,
        reportForm: {
          company: '',
          reporter: '',
          department: '',
          month: '',
          title: '',
        },
      },
      showRulesModal: false,
      rulesLoading: false,
      categoryRules: [],
      showBackupModal: false,
      backupsLoading: false,
      backupsBusy: false,
      backups: [],
      backupDir: '',
      backupMax: 10,
      showConflictModal: false,
      conflictsLoading: false,
      conflictsBusy: false,
      conflictGroups: [],
      conflictArchived: [],
      conflictKeep: {},
      showDashboardModal: false,
      dashboardLoading: false,
      dashboard: null,
      showShortcutsModal: false,
      themeMode: 'auto',
      busy: '',
      toast: { message: '', kind: 'info', timer: null },
      exportResult: null,
      exportMenuOpen: false,
      moreMenuOpen: false,
    };
  },

  computed: {
    allVisibleSelected() {
      return (
        this.invoices.length > 0 &&
        this.invoices.every((row) => this.selected.includes(row.id))
      );
    },
    activeLine() {
      return this.lines.find((line) => line.sequence === this.activeSequence) || null;
    },
    reviewPosition() {
      if (!this.reviewQueue || !this.preview) return 0;
      const index = this.reviewQueue.findIndex(
        (row) => row.id === this.preview.invoice.id
      );
      return index < 0 ? 0 : index + 1;
    },
    previewDirty() {
      const invoice = this.preview && this.preview.invoice;
      const form = this.previewForm;
      if (!invoice || !form) return false;
      return (
        (form.invoice_number || '').trim() !==
          (invoice.invoice_number || '').trim() ||
        (form.invoice_date || '') !== (invoice.invoice_date || '') ||
        (form.seller_name || '').trim() !== (invoice.seller_name || '').trim() ||
        (form.buyer_name || '').trim() !== (invoice.buyer_name || '').trim() ||
        this.amountKey(form.amount) !== this.amountKey(invoice.amount) ||
        (form.category || '').trim() !== (invoice.category || '').trim() ||
        (form.item_summary || '').trim() !== (invoice.item_summary || '').trim()
      );
    },
    mismatchCount() {
      return this.lines.filter((line) => line.amount_mismatch).length;
    },
    unlinkedCount() {
      return this.lines.filter((line) => line.has_invoice && !line.invoice_ids.length)
        .length;
    },
    mileageAmount() {
      const km = Number.parseFloat(this.mileageForm.km);
      const rate = Number.parseFloat(this.boot.mileage_rate);
      if (!Number.isFinite(km) || km <= 0 || !Number.isFinite(rate)) return '0.00';
      // Integer hundredths match the backend's Decimal half-up rounding
      // (3.33 × 1.5 must show 5.00, not 4.99 from float drift).
      const kmHundredths = Math.round(km * 100);
      const rateHundredths = Math.round(rate * 100);
      const cents = Math.floor((kmHundredths * rateHundredths + 50) / 100);
      return (cents / 100).toFixed(2);
    },
    showUnrecognizedBanner() {
      return (
        !this.unrecognizedDismissed &&
        !this.busy &&
        Number(this.boot.unrecognized_count) > 0
      );
    },
    modalOpen() {
      return (
        this.showReportModal ||
        this.showMileageModal ||
        this.showRulesModal ||
        this.showBackupModal ||
        this.showConflictModal ||
        this.showImportModal ||
        this.showDashboardModal ||
        this.showShortcutsModal ||
        this.showWizard ||
        Boolean(this.exportResult)
      );
    },
    hasReportChoices() {
      const choices = this.boot.report_choices || {};
      return Boolean(
        (choices.company && choices.company.length) ||
          (choices.department && choices.department.length) ||
          (choices.reporter && choices.reporter.length)
      );
    },
    themeLabel() {
      const labels = (window.invoiceDeskTheme && window.invoiceDeskTheme.labels) || {
        auto: '自动',
        light: '浅色',
        dark: '深色',
      };
      return labels[this.themeMode] || '自动';
    },
    categoryChoices() {
      const names = [];
      const seen = new Set();
      for (const name of this.boot.categories || []) {
        if (name && !seen.has(name)) {
          seen.add(name);
          names.push(name);
        }
      }
      for (const row of this.invoices) {
        if (row.category && !seen.has(row.category)) {
          seen.add(row.category);
          names.push(row.category);
        }
      }
      return names;
    },
    importPercent() {
      const progress = this.importProgress;
      if (!progress.total) return 0;
      return Math.min(100, Math.round((progress.done / progress.total) * 100));
    },
    importStageText() {
      const progress = this.importProgress;
      if (progress.stage === 'reparse') {
        return `正在补识别旧记录 ${progress.done}/${progress.total}`;
      }
      return `正在解析发票 ${progress.done}/${progress.total}`;
    },
    categoryMax() {
      const items = this.dashboard ? this.dashboard.categories : [];
      return Math.max(1, ...items.map((item) => Math.abs(item.total_cents)));
    },
    monthMax() {
      const items = this.dashboard ? this.dashboard.months : [];
      return Math.max(1, ...items.map((item) => Math.abs(item.total_cents)));
    },
  },

  mounted() {
    if (window.pywebview && window.pywebview.api) {
      this.load();
    } else {
      window.addEventListener('pywebviewready', () => this.load(), { once: true });
    }
    document.addEventListener('keydown', this.onKeydown);
    document.addEventListener('click', this.closeMenus);
    this.syncTheme();
  },

  beforeUnmount() {
    document.removeEventListener('keydown', this.onKeydown);
    document.removeEventListener('click', this.closeMenus);
  },

  methods: {
    /* ---------------------------------------------------------- bridge */

    async invoke(method, args, silent) {
      const bridge = window.pywebview && window.pywebview.api;
      if (!bridge || typeof bridge[method] !== 'function') {
        const message = '桌面桥接尚未就绪，请重启程序。';
        if (!silent) this.notify(message, 'error');
        throw new Error(message);
      }
      const response = await bridge[method](...args);
      if (!response || response.ok !== true) {
        const message = (response && response.error) || '操作失败';
        if (!silent) this.notify(message, 'error', 8000);
        throw new Error(message);
      }
      return response.data;
    },

    call(method, ...args) {
      return this.invoke(method, args, false);
    },

    silentCall(method, ...args) {
      return this.invoke(method, args, true);
    },

    notify(message, kind = 'info', duration = 4500) {
      if (this.toast.timer) window.clearTimeout(this.toast.timer);
      this.toast.message = message;
      this.toast.kind = kind;
      this.toast.timer = window.setTimeout(() => {
        this.toast.message = '';
        this.toast.timer = null;
      }, duration);
    },

    onKeydown(event) {
      if (event.key === 'Escape') {
        if (this.exportMenuOpen || this.moreMenuOpen) {
          this.exportMenuOpen = false;
          this.moreMenuOpen = false;
        } else if (this.exportResult) this.exportResult = null;
        else if (this.showShortcutsModal) this.showShortcutsModal = false;
        else if (this.showDashboardModal) this.showDashboardModal = false;
        else if (this.showWizard) this.closeWizard();
        else if (this.showReportModal) this.showReportModal = false;
        else if (this.showMileageModal) this.showMileageModal = false;
        else if (this.showRulesModal) this.showRulesModal = false;
        else if (this.showBackupModal) this.showBackupModal = false;
        else if (this.showConflictModal) this.showConflictModal = false;
        else if (this.showImportModal) this.showImportModal = false;
        else if (this.preview) this.closePreview();
        return;
      }

      if (
        this.preview &&
        !this.modalOpen &&
        !event.ctrlKey &&
        !event.altKey &&
        !event.metaKey &&
        !event.shiftKey &&
        (event.key === 'ArrowDown' || event.key === 'ArrowUp')
      ) {
        const target = event.target;
        const editing =
          target instanceof HTMLElement &&
          (target.matches('input, textarea, select') || target.isContentEditable);
        if (!editing) {
          event.preventDefault();
          this.stepPreview(event.key === 'ArrowDown' ? 1 : -1);
          return;
        }
      }

      const key = event.key.toLowerCase();
      const ctrl = event.ctrlKey || event.metaKey;
      if (ctrl && event.shiftKey && key === 'd') {
        event.preventDefault();
        this.cycleTheme();
        return;
      }
      if (event.key === 'F1') {
        event.preventDefault();
        if (!this.modalOpen) this.openShortcuts();
        return;
      }
      if (!ctrl || event.altKey || event.shiftKey) return;

      if (key === 'enter') {
        if (
          !this.modalOpen &&
          this.preview &&
          this.previewDirty &&
          !this.previewSaving &&
          !this.busy
        ) {
          event.preventDefault();
          this.saveInvoiceEdits();
        }
        return;
      }
      if (this.modalOpen) return;
      if (key === 'k') {
        event.preventDefault();
        this.focusSearch();
      } else if (key === 'i') {
        event.preventDefault();
        if (!this.busy) this.openImportModal();
      } else if (key === 'e') {
        event.preventDefault();
        if (this.report && !this.busy) this.exportReport();
      } else if (key === 'b') {
        event.preventDefault();
        if (this.report) this.poolVisible = !this.poolVisible;
      } else if (key === 'd') {
        event.preventDefault();
        if (!this.busy) this.openDashboard();
      }
    },

    /* --------------------------------------------------------- appearance */

    syncTheme() {
      const theme = window.invoiceDeskTheme;
      this.themeMode = theme ? theme.get() : 'auto';
    },

    cycleTheme() {
      const theme = window.invoiceDeskTheme;
      if (!theme) {
        this.notify('当前环境不支持外观切换。', 'error');
        return;
      }
      this.themeMode = theme.cycle();
      this.notify(`外观已切换为${this.themeLabel}。`, 'info', 2500);
    },

    openShortcuts() {
      this.showShortcutsModal = true;
    },

    focusSearch() {
      this.poolVisible = true;
      this.$nextTick(() => {
        const input = this.$refs.searchInput;
        if (!input) return;
        input.focus();
        input.select();
      });
    },

    /* ---------------------------------------------------------- dashboard */

    async openDashboard() {
      this.showDashboardModal = true;
      await this.loadDashboard();
    },

    async loadDashboard() {
      this.dashboardLoading = true;
      try {
        this.dashboard = await this.call('dashboard', this.filterPayload());
      } catch (error) {
        this.dashboard = null;
      } finally {
        this.dashboardLoading = false;
      }
    },

    barPercent(value, max) {
      const magnitude = Math.abs(Number(value) || 0);
      const ceiling = Math.abs(Number(max) || 0);
      if (!magnitude || !ceiling) return '0%';
      return `${Math.max(2, Math.round((magnitude / ceiling) * 100))}%`;
    },

    /* ------------------------------------------------------------- load */

    async load() {
      this.loading = true;
      this.fatal = '';
      try {
        this.boot = await this.call('bootstrap');
        this.reports = this.boot.reports;
        this.reportForm.month = this.boot.default_month;
        this.mileageForm.line_date = this.boot.today;
        this.currentReportId = this.reports.length ? this.reports[0].id : '';
        await this.loadReport();
        await this.refreshInvoices(true);
      } catch (error) {
        this.fatal = error.message || String(error);
      } finally {
        this.loading = false;
      }
    },

    async reloadBootstrap() {
      this.boot = await this.call('bootstrap');
      this.reports = this.boot.reports;
      const stillThere = this.reports.some(
        (item) => item.id === Number(this.currentReportId)
      );
      if (!stillThere) {
        this.currentReportId = this.reports.length ? this.reports[0].id : '';
      }
    },

    resetPoolFilters() {
      // A fresh report starts from a clean pool: stale filters (a batch, a
      // search, "only unlinked") would hide the invoices the user is about to
      // associate with it.
      this.filters = {
        batch_id: '',
        search: '',
        unlinked_only: false,
        unrecognized_only: false,
        include_review: true,
        date_from: '',
        date_to: '',
      };
      this.selected = [];
    },

    filterPayload() {
      return {
        batch_id:
          this.filters.batch_id === '' ? null : Number(this.filters.batch_id),
        search: this.filters.search,
        unlinked_only: this.filters.unlinked_only,
        unrecognized_only: this.filters.unrecognized_only,
        include_review: this.filters.include_review,
        date_from: this.filters.date_from,
        date_to: this.filters.date_to,
      };
    },

    async refreshInvoices(clearSelection) {
      this.invoicesLoading = true;
      try {
        const data = await this.silentCall('list_invoices', this.filterPayload());
        this.invoices = data.rows;
        this.invoiceSummary = data.summary;
        if (clearSelection) this.selected = [];
      } catch (error) {
        this.notify(error.message, 'error');
      } finally {
        this.invoicesLoading = false;
      }
    },

    async loadReport() {
      if (!this.currentReportId) {
        this.report = null;
        this.lines = [];
        this.activeSequence = null;
        return;
      }
      try {
        const data = await this.call('get_report', Number(this.currentReportId));
        this.report = data.report;
        this.lines = data.lines;
        this.reports = this.reports.map((item) =>
          item.id === data.report.id ? { ...item, ...data.report } : item
        );
        if (!this.lines.some((line) => line.sequence === this.activeSequence)) {
          this.activeSequence = this.lines.length ? this.lines[0].sequence : null;
        }
      } catch (error) {
        this.report = null;
        this.lines = [];
        this.notify(error.message, 'error');
      }
    },

    async refreshReports() {
      this.reports = await this.call('list_reports');
      if (!this.reports.some((item) => item.id === Number(this.currentReportId))) {
        this.currentReportId = this.reports.length ? this.reports[0].id : '';
      }
    },

    /* ----------------------------------------------------------- report */

    openReportModal(mode) {
      this.reportModalMode = mode;
      if (mode === 'edit' && this.report) {
        this.reportForm = {
          company: this.report.company,
          reporter: this.report.reporter,
          month: this.report.month,
          department: this.report.department,
          title: this.report.title,
          note: this.report.note,
        };
      } else {
        this.reportForm = {
          company: this.report ? this.report.company : '',
          reporter: this.report ? this.report.reporter : '',
          month: this.boot.default_month,
          department: this.report ? this.report.department : '',
          title: '',
          note: '',
        };
      }
      this.showReportModal = true;
    },

    async submitReport() {
      const form = this.reportForm;
      if (!form.company || !form.reporter || !form.month) {
        this.notify('公司、报销人和月份不能为空。', 'error');
        return;
      }
      this.busy = '正在保存…';
      let created = false;
      try {
        if (this.reportModalMode === 'create') {
          const data = await this.call('create_report', { ...form });
          this.currentReportId = data.report_id;
          this.resetPoolFilters();
          created = true;
        } else {
          await this.call('update_report', Number(this.currentReportId), {
            ...form,
          });
        }
        this.showReportModal = false;
        await this.refreshReports();
        await this.loadReport();
        if (created) await this.refreshInvoices(true);
        this.notify('报销单已保存。', 'success');
      } catch (error) {
        /* call() already surfaced the message. */
      } finally {
        this.busy = '';
      }
    },

    async saveLine(line) {
      try {
        await this.call('update_line', Number(this.currentReportId), line.sequence, {
          line_date: line.line_date,
          category: line.category,
          description: line.description,
          amount: line.amount,
          has_invoice: line.has_invoice,
          invoice_note: line.invoice_note,
          project_name: line.project_name,
        });
        await this.loadReport();
      } catch (error) {
        await this.loadReport();
      }
    },

    async addBlankLine() {
      if (!this.report) return;
      try {
        await this.call('add_line', Number(this.currentReportId), {
          line_date: `${this.report.month}-01`,
          category: this.boot.default_category,
          description: '',
          amount: '0.00',
          has_invoice: false,
        });
        await this.loadReport();
        this.activeSequence = this.lines.length
          ? this.lines[this.lines.length - 1].sequence
          : null;
        this.notify('已新增一行，可直接编辑。', 'success');
      } catch (error) {
        /* surfaced already */
      }
    },

    async removeLine(line) {
      if (!window.confirm(`删除第 ${line.sequence} 行「${line.category}」？`)) return;
      try {
        await this.call('remove_line', Number(this.currentReportId), line.sequence);
        await this.loadReport();
        this.notify('已删除。', 'success');
      } catch (error) {
        /* surfaced already */
      }
    },

    async addSelectedInvoices() {
      const ids = [...this.selected];
      if (!ids.length || !this.currentReportId) return;
      this.busy = `正在生成 ${ids.length} 行…`;
      try {
        const data = await this.call(
          'add_lines_from_invoices',
          Number(this.currentReportId),
          ids,
          this.quickProject
        );
        await this.loadReport();
        await this.refreshInvoices(false);
        this.selected = [];
        const parts = [`已生成 ${data.added} 行`];
        if (data.skipped) {
          parts.push(
            `跳过 ${data.skipped} 张（已关联 ${data.skipped_linked}、` +
              `缺金额 ${data.skipped_missing_amount}、缺日期 ${data.skipped_missing_date}）`
          );
        }
        this.notify(parts.join('；'), data.added ? 'success' : 'error', 7000);
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
    },

    async linkToActiveLine() {
      const line = this.activeLine;
      if (!line || !this.selected.length) return;
      await this.linkInvoices(line, [...this.selected]);
    },

    async linkInvoices(line, invoiceIds) {
      try {
        const data = await this.call(
          'link_invoices',
          Number(this.currentReportId),
          line.sequence,
          invoiceIds
        );
        await this.loadReport();
        await this.refreshInvoices(false);
        this.notify(
          data.added
            ? `已关联 ${data.added} 张发票到第 ${line.sequence} 行。`
            : '这些发票已经关联过。',
          data.added ? 'success' : 'info'
        );
      } catch (error) {
        /* surfaced already */
      }
    },

    async unlinkInvoice(line, invoiceId) {
      if (!window.confirm(`取消第 ${line.sequence} 行与发票 #${invoiceId} 的关联？`)) {
        return;
      }
      try {
        await this.call(
          'unlink_invoices',
          Number(this.currentReportId),
          line.sequence,
          [invoiceId]
        );
        await this.loadReport();
        await this.refreshInvoices(false);
        this.notify('已取消关联。', 'success');
      } catch (error) {
        /* surfaced already */
      }
    },

    /* ---------------------------------------------------------- mileage */

    openMileageModal() {
      this.mileageForm = {
        line_date: this.report ? `${this.report.month}-01` : this.boot.today,
        km: '',
        description: '',
        category: '汽车费用',
        project_name: this.activeLine ? this.activeLine.project_name : '',
      };
      this.showMileageModal = true;
    },

    async submitMileage() {
      this.busy = '正在添加里程行…';
      try {
        const data = await this.call(
          'add_mileage',
          Number(this.currentReportId),
          { ...this.mileageForm }
        );
        this.showMileageModal = false;
        await this.loadReport();
        this.notify(`已添加里程行：${data.km} km，¥${data.amount}。`, 'success');
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
    },

    /* ----------------------------------------------------------- import */

    openImportModal() {
      const recent = this.boot.recent_import_paths || [];
      this.importForm = { path: recent.length ? recent[0] : '', label: '' };
      this.importPlan = null;
      this.showImportModal = true;
    },

    resetImportPlan() {
      this.importPlan = null;
    },

    folderName(path) {
      const text = String(path == null ? '' : path).replace(/[\\/]+$/, '');
      const parts = text.split(/[\\/]/);
      return parts[parts.length - 1] || text;
    },

    async pickFolder() {
      if (this.pickingFolder) return;
      this.pickingFolder = true;
      try {
        const data = await this.call('pick_folder', this.importForm.path || '');
        if (data && data.path) this.importForm.path = data.path;
      } catch (error) {
        /* call() already surfaced the message. */
      } finally {
        this.pickingFolder = false;
      }
    },

    async runImportJob(path, label, token) {
      const started = await this.call(
        'import_start',
        path || '',
        label || '',
        token || ''
      );
      const jobId = started.job_id;
      this.importProgress = {
        active: true,
        stage: 'parse',
        done: 0,
        total: started.total || 0,
        name: '',
        cancelling: false,
      };
      let state = null;
      try {
        for (;;) {
          await new Promise((resolve) => window.setTimeout(resolve, 400));
          state = await this.silentCall('import_status', jobId);
          if (!state || state.status === 'idle') {
            throw new Error('导入状态读取失败');
          }
          if (state.status !== 'running') break;
          this.importProgress = {
            active: true,
            stage: state.stage || 'parse',
            done: state.done || 0,
            total: state.total || 0,
            name: state.name || '',
            cancelling: this.importProgress.cancelling,
          };
        }
      } finally {
        this.importProgress = {
          active: false,
          stage: '',
          done: 0,
          total: 0,
          name: '',
          cancelling: false,
        };
      }
      if (state.status === 'cancelled') {
        this.notify('已取消导入，数据库没有写入任何数据。', 'info', 8000);
        return null;
      }
      if (state.status === 'error') {
        this.notify(state.error || '导入失败，请查看控制台日志。', 'error', 9000);
        return null;
      }
      return state.summary;
    },

    async runImport(path, label, token) {
      const data = await this.runImportJob(path, label, token);
      if (!data) return null;
      this.unrecognizedDismissed = false;
      await this.reloadBootstrap();
      if (data.batch_id != null) {
        // Focus the fresh batch so the just-imported rows — company and
        // category — are what the user sees first.
        this.filters.batch_id = String(data.batch_id);
        this.poolVisible = true;
      }
      await this.refreshInvoices(true);
      return data;
    },

    importMessage(data) {
      let message =
        `导入完成：新增 ${data.invoices_imported} 张，` +
        `重复 ${data.invoices_duplicate}，冲突 ${data.invoices_conflict}，` +
        `待确认 ${data.needs_review}，合计 ¥${data.amount}。`;
      if (data.cancelled) {
        message += '已提前停止补识别。';
      }
      if (data.reparsed) {
        message += `已自动补识别旧记录 ${data.reparsed} 张。`;
      }
      if (data.reparse_failed) {
        message += `仍有 ${data.reparse_failed} 张未识别，可在顶部提示条重试。`;
      }
      return message;
    },

    async submitImport() {
      if (!this.importForm.path) {
        this.notify('请先选择发票文件夹。', 'error');
        return;
      }
      this.busy = '正在预检文件夹…';
      try {
        const plan = await this.call(
          'import_preflight',
          this.importForm.path,
          this.importForm.label
        );
        if (!plan.new_files && !plan.reparse_candidates) {
          this.notify(
            '没有新发票，也没有可补识别的旧记录；源文件不会被修改。',
            'info',
            8000
          );
          return;
        }
        this.importPlan = plan;
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
    },

    async confirmImport() {
      const plan = this.importPlan;
      if (!plan) return;
      this.busy = '正在导入发票…';
      try {
        const data = await this.runImport(
          this.importForm.path,
          this.importForm.label,
          plan.token
        );
        if (!data) return;
        this.showImportModal = false;
        this.notify(this.importMessage(data), 'success', 10000);
      } catch (error) {
        /* surfaced already */
      } finally {
        this.importPlan = null;
        this.busy = '';
      }
    },

    async cancelImport() {
      if (this.importProgress.cancelling) return;
      this.importProgress = { ...this.importProgress, cancelling: true };
      try {
        await this.silentCall('cancel_import');
      } catch (error) {
        /* surfaced already */
      }
    },

    async reparseUnrecognized() {
      this.busy = '正在重新识别图片发票，请稍候…';
      try {
        const data = await this.call('reparse_unrecognized');
        await this.reloadBootstrap();
        this.unrecognizedDismissed = false;
        await this.refreshInvoices(false);
        const parts = [`重新识别成功 ${data.reparsed} 张`];
        if (data.failed) parts.push(`仍未识别 ${data.failed} 张`);
        if (data.skipped_edited) parts.push(`跳过已人工修正 ${data.skipped_edited} 张`);
        if (data.skipped_missing) parts.push(`缺源文件 ${data.skipped_missing} 张`);
        if (data.conflicts) parts.push(`${data.conflicts} 张与既有票号冲突，已标记待确认`);
        this.notify(parts.join('；'), data.reparsed ? 'success' : 'info', 9000);
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
    },

    /* ----------------------------------------------------------- wizard */

    openWizard() {
      const recent = this.boot.recent_import_paths || [];
      const report = this.report;
      this.wizard = {
        month: this.boot.default_month,
        folder: recent.length ? recent[0] : '',
        label: '',
        importResult: null,
        batchId: null,
        reportId: '',
        reportSummary: '',
        addedCount: 0,
        skipped: null,
        reportForm: {
          company: report ? report.company : '',
          reporter: report ? report.reporter : '',
          department: report ? report.department : '',
          month: this.boot.default_month,
          title: '',
        },
      };
      this.wizardStep = 1;
      this.wizardStats = {
        loading: false,
        count: 0,
        total: '0.00',
        review_count: 0,
        missing_amount_count: 0,
        categories: [],
        unlinked_ids: [],
      };
      this.showWizard = true;
    },

    closeWizard() {
      this.showWizard = false;
    },

    async pickWizardFolder() {
      if (this.pickingWizardFolder) return;
      this.pickingWizardFolder = true;
      try {
        const data = await this.call('pick_folder', this.wizard.folder || '');
        if (data && data.path) this.wizard.folder = data.path;
      } catch (error) {
        /* call() already surfaced the message */
      } finally {
        this.pickingWizardFolder = false;
      }
    },

    async wizardImport() {
      if (!this.wizard.folder) {
        this.notify('请先选择发票文件夹。', 'error');
        return;
      }
      this.wizardBusy = true;
      this.busy = '正在导入发票，请稍候…';
      try {
        const data = await this.runImport(this.wizard.folder, this.wizard.label);
        if (!data) return;
        this.wizard.importResult = data;
        this.wizard.batchId = data.batch_id;
        if (!this.wizard.reportForm.month) {
          this.wizard.reportForm.month = this.wizard.month;
        }
        await this.wizardRefreshStats();
        this.notify(this.importMessage(data), 'success', 10000);
      } catch (error) {
        /* call() already surfaced the message */
      } finally {
        this.wizardBusy = false;
        this.busy = '';
      }
    },

    async wizardRefreshStats() {
      if (this.wizard.batchId == null) return;
      this.wizardStats = { ...this.wizardStats, loading: true };
      try {
        const filters = {
          batch_id: Number(this.wizard.batchId),
          date_from: '',
          date_to: '',
          include_review: true,
        };
        const [dash, unlinked] = await Promise.all([
          this.call('dashboard', filters),
          this.silentCall('list_invoices', { ...filters, unlinked_only: true }),
        ]);
        this.wizardStats = {
          loading: false,
          count: dash.count,
          total: dash.total,
          review_count: dash.review_count,
          missing_amount_count: dash.missing_amount_count,
          categories: dash.categories,
          unlinked_ids: unlinked.rows.map((row) => row.id),
        };
      } catch (error) {
        this.wizardStats = { ...this.wizardStats, loading: false };
      }
    },

    async wizardStartReview() {
      if (this.wizard.batchId == null) return;
      this.showWizard = false;
      await this.openReviewQueue({
        batchId: Number(this.wizard.batchId),
        resume: true,
      });
    },

    wizardGo(step) {
      const target = Number(step);
      // Completed steps can be reopened; later ones unlock in order.
      if (target < 1 || target > this.wizardStep) return;
      this.wizardStep = target;
      if (target === 2 || target === 3) this.wizardRefreshStats();
    },

    wizardNext(step) {
      if (Number(step) <= this.wizardStep) return;
      this.wizardStep = Number(step);
      if (this.wizardStep === 2 || this.wizardStep === 3) {
        this.wizardRefreshStats();
      }
    },

    async wizardCreateReport() {
      const form = this.wizard.reportForm;
      if (!form.company || !form.reporter || !form.month) {
        this.notify('公司、报销人和月份不能为空。', 'error');
        return;
      }
      this.wizardBusy = true;
      this.busy = '正在生成报销单…';
      try {
        const created = await this.call('create_report', { ...form });
        this.wizard.reportId = created.report_id;
        this.currentReportId = created.report_id;
        this.wizard.reportSummary =
          `${form.company} / ${form.reporter} / ${form.month}`;
        let added = 0;
        let skipped = null;
        const ids = this.wizardStats.unlinked_ids;
        if (ids.length) {
          const result = await this.call(
            'add_lines_from_invoices',
            created.report_id,
            ids,
            ''
          );
          added = result.added;
          skipped = result.skipped ? result : null;
        }
        this.wizard.addedCount = added;
        this.wizard.skipped = skipped;
        await this.refreshReports();
        await this.loadReport();
        await this.refreshInvoices(false);
        const parts = [`报销单已生成，新增 ${added} 行`];
        if (skipped) {
          parts.push(
            `跳过 ${skipped.skipped} 张（已关联 ${skipped.skipped_linked}、` +
              `缺金额 ${skipped.skipped_missing_amount}、` +
              `缺日期 ${skipped.skipped_missing_date}）`
          );
        }
        this.notify(parts.join('；'), 'success', 9000);
      } catch (error) {
        /* call() already surfaced the message */
      } finally {
        this.wizardBusy = false;
        this.busy = '';
      }
    },

    async wizardExportReport() {
      if (!this.wizard.reportId) return;
      this.currentReportId = this.wizard.reportId;
      if (!this.report || this.report.id !== Number(this.wizard.reportId)) {
        await this.loadReport();
      }
      await this.exportReport();
    },

    async wizardExportEvidence() {
      if (this.wizard.batchId == null) return;
      await this.runExportEvidence({
        batch_id: Number(this.wizard.batchId),
        include_review: true,
      });
    },

    /* ------------------------------------------------------------ rules */

    async openRulesModal() {
      this.showRulesModal = true;
      await this.loadRules();
    },

    async loadRules() {
      this.rulesLoading = true;
      try {
        const data = await this.call('list_rules');
        this.categoryRules = data.rules;
      } catch (error) {
        /* surfaced already */
      } finally {
        this.rulesLoading = false;
      }
    },

    async toggleRule(rule) {
      const label = this.displayText(rule.pattern, 40);
      try {
        await this.call('set_rule_enabled', rule.id, !rule.enabled);
        await this.loadRules();
        this.notify(
          `已${rule.enabled ? '停用' : '启用'}规则「${label}」。`,
          'success'
        );
      } catch (error) {
        /* surfaced already */
      }
    },

    async backfillRule(rule) {
      if (this.previewDirty) {
        this.notify('请先保存或关闭当前发票的修正，再回溯分类。', 'error');
        return;
      }
      try {
        const preview = await this.call('preview_rule_backfill', rule.id);
        if (!preview.update_count) {
          this.notify(
            `没有需要更新的发票（同分类 ${preview.unchanged} 张，` +
              `人工分类跳过 ${preview.manual_skipped} 张）。`,
            'info'
          );
          return;
        }
        const label = this.displayText(rule.pattern, 40);
        const category = this.displayText(preview.rule.category, 20);
        const samples = preview.samples
          .slice(0, 3)
          .map(
            (item) =>
              `  #${item.id} ${this.displayText(item.invoice_date, 16)} ` +
              `${this.displayText(item.category, 20)} → ${category}`
          );
        const lines = [
          `规则「${label}」→「${category}」`,
          `将更新 ${preview.update_count} 张发票；同分类 ${preview.unchanged} 张、` +
            `人工分类 ${preview.manual_skipped} 张会跳过。`,
          '每张发票都会留下修正记录；待确认标记和识别警告不会被清除。',
        ];
        if (samples.length) lines.push('示例：', ...samples);
        lines.push('确定应用到历史发票？');
        if (!window.confirm(lines.join('\n'))) return;
        this.busy = '正在回溯分类…';
        const data = await this.call(
          'apply_rule_backfill',
          rule.id,
          preview.token
        );
        await this.loadRules();
        await this.refreshInvoices(false);
        if (this.preview) {
          const refreshed = this.invoices.find(
            (row) => row.id === this.preview.invoice.id
          );
          if (refreshed) {
            this.preview = { ...this.preview, invoice: refreshed };
            this.previewForm = this.invoiceForm(refreshed);
          }
        }
        this.notify(
          `已更新 ${data.updated} 张发票的分类，逐张留有修正记录。`,
          'success',
          8000
        );
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
    },

    async deleteRule(rule) {
      if (
        !window.confirm(
          `删除规则「${this.displayText(rule.pattern, 40)}」？\n` +
            '删除后新导入不再自动分类，已分类的发票保持不变。'
        )
      ) {
        return;
      }
      try {
        await this.call('delete_rule', rule.id);
        await this.loadRules();
        this.notify('规则已删除。', 'success');
      } catch (error) {
        /* surfaced already */
      }
    },

    /* ----------------------------------------------------------- export */

    toggleExportMenu() {
      this.exportMenuOpen = !this.exportMenuOpen;
      this.moreMenuOpen = false;
    },

    toggleMoreMenu() {
      this.moreMenuOpen = !this.moreMenuOpen;
      this.exportMenuOpen = false;
    },

    closeMenus(event) {
      if (!this.exportMenuOpen && !this.moreMenuOpen) return;
      const target = event && event.target;
      if (target && typeof target.closest === 'function') {
        if (target.closest('.export-menu-wrap') && this.exportMenuOpen) return;
        if (target.closest('.more-menu-wrap') && this.moreMenuOpen) return;
      }
      this.exportMenuOpen = false;
      this.moreMenuOpen = false;
    },

    exportReportFromMenu() {
      this.exportMenuOpen = false;
      this.exportReport();
    },

    exportEvidenceFromMenu() {
      this.exportMenuOpen = false;
      this.exportEvidence();
    },

    openDashboardFromMenu() {
      this.moreMenuOpen = false;
      this.openDashboard();
    },

    openRulesFromMenu() {
      this.moreMenuOpen = false;
      this.openRulesModal();
    },

    openBackupFromMenu() {
      this.moreMenuOpen = false;
      this.openBackupModal();
    },

    openConflictsFromMenu() {
      this.moreMenuOpen = false;
      this.openConflictModal();
    },

    openConflictModal() {
      this.showConflictModal = true;
      this.loadConflicts();
    },

    async loadConflicts() {
      this.conflictsLoading = true;
      try {
        const data = await this.call('list_conflicts');
        this.conflictGroups = data.groups;
        this.conflictArchived = data.archived;
        const keep = {};
        for (const group of data.groups) {
          // Prefer a record the user already reviewed, else the newest row
          // (the API lists newest first) so the radio always has a default.
          const confirmed = group.invoices.find((row) => !row.needs_review);
          keep[group.invoice_number] = (confirmed || group.invoices[0]).id;
        }
        this.conflictKeep = keep;
      } catch (error) {
        /* surfaced already */
      } finally {
        this.conflictsLoading = false;
      }
    },

    conflictArchiveIds(group) {
      const keepId = Number(this.conflictKeep[group.invoice_number]);
      return group.invoices
        .filter((row) => row.id !== keepId)
        .map((row) => row.id);
    },

    async resolveConflict(group) {
      const keepId = Number(this.conflictKeep[group.invoice_number]);
      const archiveIds = this.conflictArchiveIds(group);
      if (!keepId || !archiveIds.length) {
        this.notify('请选择要保留的那一张。', 'error');
        return;
      }
      const keepRow = group.invoices.find((row) => row.id === keepId);
      const linked = group.invoices.filter(
        (row) => row.id !== keepId && row.linked
      );
      if (linked.length) {
        this.notify(
          '要归档的发票已关联报销单，请先从报销单移除再归档。',
          'error',
          8000
        );
        return;
      }
      if (
        this.preview &&
        this.previewDirty &&
        archiveIds.includes(this.preview.invoice.id)
      ) {
        this.notify('当前发票有未保存的修正，请先保存或关闭预览。', 'error');
        return;
      }
      const confirmed = window.confirm(
        `保留 #${keepId}（${this.displayText(keepRow.seller_name, 20)} · ` +
          `${keepRow.amount || '金额缺失'}），归档其余 ${archiveIds.length} 张？\n\n` +
          '归档不会删除源发票文件，之后可在“已归档”中恢复。'
      );
      if (!confirmed) return;
      this.conflictsBusy = true;
      try {
        const data = await this.call(
          'resolve_conflict',
          keepId,
          archiveIds
        );
        if (this.preview) {
          if (data.archived_ids.includes(this.preview.invoice.id)) {
            this.preview = null;
            this.previewForm = null;
            this.invoiceEdits = [];
          } else if (this.preview.invoice.id === data.invoice.id) {
            this.preview = { ...this.preview, invoice: data.invoice };
            this.previewForm = this.invoiceForm(data.invoice);
            this.invoiceEdits = data.edits;
          }
        }
        await this.loadConflicts();
        await this.refreshInvoices(false);
        await this.reloadBootstrap();
        this.notify(
          `已保留 #${keepId}，归档 ${data.archived_ids.length} 张。`,
          'success',
          7000
        );
      } catch (error) {
        /* surfaced already */
      } finally {
        this.conflictsBusy = false;
      }
    },

    async restoreArchived(row) {
      const confirmed = window.confirm(
        `恢复 #${row.id}（票号 ${row.invoice_number || '无'}）？\n\n` +
          '如果仍有同号有效发票，恢复后会自动标记为冲突。'
      );
      if (!confirmed) return;
      this.conflictsBusy = true;
      try {
        const data = await this.call('restore_invoice', row.id);
        await this.loadConflicts();
        await this.refreshInvoices(false);
        await this.reloadBootstrap();
        this.notify(
          data.conflict_warning
            ? `已恢复 #${row.id}，但仍与同号发票冲突，请再次核对。`
            : `已恢复 #${row.id}。`,
          data.conflict_warning ? 'info' : 'success',
          8000
        );
      } catch (error) {
        /* surfaced already */
      } finally {
        this.conflictsBusy = false;
      }
    },

    openBackupModal() {
      this.showBackupModal = true;
      this.loadBackups();
    },

    async loadBackups() {
      this.backupsLoading = true;
      try {
        const data = await this.call('list_backups');
        this.backups = data.backups;
        this.backupDir = data.dir;
        this.backupMax = data.max;
      } catch (error) {
        /* surfaced already */
      } finally {
        this.backupsLoading = false;
      }
    },

    async createBackup() {
      this.backupsBusy = true;
      try {
        const data = await this.call('create_backup');
        await this.loadBackups();
        this.notify(`已创建备份：${data.created_at}`, 'success');
      } catch (error) {
        /* surfaced already */
      } finally {
        this.backupsBusy = false;
      }
    },

    async openBackupFolder() {
      try {
        await this.call('open_backup_folder');
      } catch (error) {
        /* surfaced already */
      }
    },

    async restoreBackup(item) {
      const confirmed = window.confirm(
        `恢复备份「${item.created_at} · ${item.label}」？\n\n` +
          '当前数据库会先另存为“恢复前”备份，然后用所选备份覆盖。\n' +
          '源发票文件不会被修改或删除。'
      );
      if (!confirmed) return;
      this.backupsBusy = true;
      this.busy = '正在恢复备份…';
      try {
        const data = await this.call('restore_backup', item.name);
        // Row ids come from the restored file; drop anything that cached them.
        this.preview = null;
        this.selected = [];
        this.currentReportId = '';
        this.report = null;
        this.lines = [];
        await this.load();
        await this.loadBackups();
        this.notify(`已恢复 ${data.restored.created_at} 的备份。`, 'success', 6000);
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
        this.backupsBusy = false;
      }
    },

    cycleThemeFromMenu() {
      this.moreMenuOpen = false;
      this.cycleTheme();
    },

    openShortcutsFromMenu() {
      this.moreMenuOpen = false;
      this.openShortcuts();
    },

    async exportReport() {
      if (!this.report) return;
      this.busy = '正在导出报销单…';
      try {
        const data = await this.call(
          'export_report',
          Number(this.currentReportId),
          this.layout,
          ''
        );
        const notes = [];
        if (data.unlinked) notes.push(`${data.unlinked} 行有票但未关联发票`);
        if (data.mismatches) notes.push(`${data.mismatches} 行金额与发票合计不一致`);
        this.exportResult = {
          title: '报销单已导出',
          summary: `共 ${data.line_count} 行，合计 ¥${data.amount}。`,
          note: notes.length ? `注意：${notes.join('；')}` : '',
          kind: 'report',
          items: [{ label: 'Excel 文件', path: data.path }],
        };
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
    },

    async exportEvidence() {
      await this.runExportEvidence(this.filterPayload());
    },

    async runExportEvidence(filters) {
      this.busy = '正在导出证据包和分类 PDF…';
      try {
        const data = await this.call(
          'export_evidence',
          filters,
          this.report ? this.report.company : ''
        );
        const items = [
          { label: 'Excel 明细', path: data.excel_path },
          { label: '打印 PDF（审计用，带标签）', path: data.pdf_path },
        ];
        if (data.group_dir) {
          items.push({
            label: `分类 PDF 文件夹（${data.group_count} 个）`,
            path: data.group_dir,
          });
        }
        for (const merged of data.merged || []) {
          items.push({
            label: `总合并 PDF（${merged.company}，${merged.pages} 页，¥${merged.total}）`,
            path: merged.path,
          });
        }
        this.exportResult = {
          title: '证据包已导出',
          summary:
            `共 ${data.count} 张发票（待确认 ${data.review_count}），` +
            `合计 ¥${data.total}；打印 PDF ${data.pages} 页，` +
            `分类 PDF（按公司/分类，A4 二合一，无标注）${data.group_count || 0} 个，` +
            `总合并 PDF ${(data.merged || []).length} 个。`,
          note: data.notice_only ? '部分票面为通知单，PDF 中已按唯一票面去重。' : '',
          kind: 'export',
          items,
          groups: data.groups || [],
        };
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
    },

    async openExportFolder() {
      if (!this.exportResult) return;
      try {
        await this.call('open_output_folder', this.exportResult.kind);
      } catch (error) {
        /* surfaced already */
      }
    },

    /* ------------------------------------------------------ category edit */

    startCategoryEdit(row) {
      if (this.categorySaving) return;
      this.editingCategoryId = row.id;
      this.categoryDraft = row.category || '';
      this.$nextTick(() => {
        // A ref inside v-for collects into an array; only one editor exists.
        const refs = this.$refs.categorySelect;
        const select = Array.isArray(refs) ? refs[0] : refs;
        if (select) select.focus();
      });
    },

    cancelCategoryEdit(row) {
      if (this.editingCategoryId === row.id) this.editingCategoryId = null;
    },

    async commitCategory(row, event) {
      const raw = event && event.target ? event.target.value : this.categoryDraft;
      const value = String(raw || '').trim();
      this.editingCategoryId = null;
      if (!value || value === (row.category || '') || this.categorySaving) return;
      const previous = row.category;
      const seller = row.seller_name || '该销售方';
      row.category = value;
      this.categorySaving = true;
      try {
        const data = await this.call('update_invoice', row.id, { category: value });
        if (this.preview && this.preview.invoice.id === row.id) {
          this.preview = { ...this.preview, invoice: data.invoice };
          this.previewForm = { ...this.previewForm, category: data.invoice.category };
          this.invoiceEdits = data.edits;
        }
        await this.refreshInvoices(false);
        let message =
          `已把「${this.displayText(seller, 40)}」归入「` +
          `${this.displayText(data.invoice.category, 20)}」。`;
        if (data.rule) message += this.ruleNotice(data.rule);
        if (data.invoice.linked) {
          message += ' 该票已关联报销单；报销行的费用名称需在报销单中修改。';
        }
        this.notify(message, 'success', 9000);
      } catch (error) {
        row.category = previous;
        /* call() already surfaced the message */
      } finally {
        this.categorySaving = false;
      }
    },

    /* ----------------------------------------------------- review queue */

    async openReviewQueue(options) {
      if (this.preview && this.previewDirty) {
        this.notify('请先保存或关闭当前发票的修正，再进入待确认队列。', 'error');
        return;
      }
      const opts = options || {};
      const scoped = opts.batchId !== undefined && opts.batchId !== null;
      const batchId = scoped
        ? Number(opts.batchId)
        : this.filters.batch_id === ''
          ? null
          : Number(this.filters.batch_id);
      this.busy = '正在整理待确认队列…';
      try {
        const data = await this.call('list_invoices', {
          batch_id: batchId,
          date_from: scoped ? '' : this.filters.date_from,
          date_to: scoped ? '' : this.filters.date_to,
          include_review: true,
          review_only: true,
        });
        if (!data.rows.length) {
          this.notify('没有待确认的发票。', 'info');
          if (opts.resume) this.resumeWizard();
          return;
        }
        this.reviewQueue = data.rows;
        this.reviewConfirmed = [];
        this.reviewResume = Boolean(opts.resume);
        await this.openPreview(data.rows[0]);
        this.notify(
          `待确认队列共 ${data.rows.length} 张：确认无误后自动下一张。`,
          'info',
          5000
        );
      } catch (error) {
        /* call() already surfaced the message */
        if (opts.resume) this.resumeWizard();
      } finally {
        this.busy = '';
      }
    },

    reviewReason(row) {
      if (!row) return '';
      const parts = [];
      if (row.warning_text) parts.push(row.warning_text);
      if (row.confidence_notes) parts.push(`低置信：${row.confidence_notes}`);
      if (!row.invoice_number) parts.push('缺票号');
      if (!row.invoice_date) parts.push('缺日期');
      if (!row.amount) parts.push('缺金额');
      return parts.length ? parts.join('；') : '需要人工核对';
    },

    async skipReview() {
      if (!this.reviewQueue || !this.reviewQueue.length) return;
      await this.stepPreview(1);
    },

    async advanceReview(confirmedRow) {
      const queue = this.reviewQueue || [];
      const index = queue.findIndex((row) => row.id === confirmedRow.id);
      if (index >= 0) {
        this.reviewConfirmed.push({ row: confirmedRow, index });
        queue.splice(index, 1);
      }
      if (!queue.length) {
        // Keep the queue bar so the last confirmation can still be undone.
        this.notify('待确认队列已全部确认。', 'success', 6000);
        return;
      }
      const next = queue[Math.min(Math.max(index, 0), queue.length - 1)];
      await this.openPreview(next);
    },

    async undoReview() {
      if (!this.reviewConfirmed.length || this.reviewSaving) return;
      const last = this.reviewConfirmed[this.reviewConfirmed.length - 1];
      this.reviewSaving = true;
      try {
        const data = await this.call('reopen_invoice', last.row.id);
        this.reviewConfirmed.pop();
        const queue = this.reviewQueue || [];
        queue.splice(Math.min(last.index, queue.length), 0, data.invoice);
        await this.refreshInvoices(false);
        await this.openPreview(data.invoice);
        this.notify('已撤销确认，这张发票回到待确认队列。', 'success');
      } catch (error) {
        /* call() already surfaced the message */
      } finally {
        this.reviewSaving = false;
      }
    },

    exitReviewQueue() {
      const resume = this.reviewResume;
      this.reviewQueue = null;
      this.reviewConfirmed = [];
      this.reviewResume = false;
      if (!resume) return;
      this.preview = null;
      this.previewForm = null;
      this.invoiceEdits = [];
      this.resumeWizard();
    },

    resumeWizard() {
      this.showWizard = true;
      this.wizardStep = 2;
      this.wizardRefreshStats();
    },

    /* ---------------------------------------------------------- preview */

    async openPreview(row) {
      if (
        this.reviewQueue &&
        !this.reviewQueue.some((item) => item.id === row.id)
      ) {
        // The queue drives its own order; jumping to another pool row would
        // silently end the queue and lose the in-session undo history.
        this.notify(
          '正在逐个确认：请先点队列条上的「×」退出，再查看其他发票。',
          'info',
          4000
        );
        return;
      }
      if (this.preview && this.previewDirty) {
        if (this.preview.invoice.id === row.id) return;
        if (!window.confirm('当前发票有未保存的修正，确定切换？')) return;
      }
      this.preview = {
        invoice: row,
        data_url: '',
        pageLabel: `${row.page_index + 1}/${row.page_count || 1}`,
        loading: true,
        error: '',
      };
      this.previewForm = this.invoiceForm(row);
      this.invoiceEdits = [];
      await this.reloadPreview(row);
      await this.loadInvoiceEdits(row.id);
    },

    amountKey(value) {
      const text = String(value == null ? '' : value)
        .trim()
        .replace(/,/g, '')
        .replace(/[¥￥]/g, '');
      if (!text) return '';
      const number = Number(text);
      return Number.isFinite(number) ? number.toFixed(2) : text;
    },

    invoiceForm(row) {
      return {
        invoice_number: row.invoice_number || '',
        invoice_date: row.invoice_date || '',
        seller_name: row.seller_name || '',
        buyer_name: row.buyer_name || '',
        amount: row.amount || '',
        category: row.category || '',
        item_summary: row.item_summary || '',
      };
    },

    async loadInvoiceEdits(invoiceId) {
      try {
        const data = await this.silentCall('get_invoice', invoiceId);
        if (!this.preview || this.preview.invoice.id !== invoiceId) return;
        this.invoiceEdits = data.edits;
      } catch (error) {
        /* history is nice-to-have; the preview must still render */
      }
    },

    ruleNotice(rule) {
      if (!rule) return '';
      const label = this.displayText(rule.pattern, 40);
      const category = this.displayText(rule.category, 20);
      return rule.enabled
        ? ` 已记住：以后导入「${label}」自动归入「${category}」。`
        : ` 规则「${label}」当前已停用，本次只改这张发票；启用后才会影响新导入。`;
    },

    async saveInvoiceEdits() {
      if (!this.preview || !this.previewForm || !this.previewDirty) return;
      this.previewSaving = true;
      try {
        const data = await this.call(
          'update_invoice',
          this.preview.invoice.id,
          { ...this.previewForm }
        );
        this.preview = { ...this.preview, invoice: data.invoice };
        this.previewForm = this.invoiceForm(data.invoice);
        this.invoiceEdits = data.edits;
        await this.refreshInvoices(false);
        let message = `已保存 ${data.changed} 项修正，源文件未改动。`;
        if (data.conflict) {
          message += ' 票号与既有发票重复或冲突，双方已标记待确认，请核对。';
        }
        if (data.rule) message += this.ruleNotice(data.rule);
        this.notify(message, data.conflict ? 'info' : 'success', 9000);
      } catch (error) {
        /* call() already surfaced the message */
      } finally {
        this.previewSaving = false;
      }
    },

    async confirmInvoice() {
      if (!this.preview || this.previewDirty) return;
      this.previewSaving = true;
      try {
        const data = await this.call(
          'confirm_invoice',
          this.preview.invoice.id
        );
        this.preview = { ...this.preview, invoice: data.invoice };
        this.previewForm = this.invoiceForm(data.invoice);
        this.invoiceEdits = data.edits;
        await this.refreshInvoices(false);
        this.notify('已标记为已核对；识别警告与低置信字段仍保留。', 'success');
        if (this.reviewQueue) {
          // The queue own its own ordering: drop the confirmed row and jump
          // to the next one without re-filtering the pool.
          await this.advanceReview(data.invoice);
          return;
        }
        const index = this.invoices.findIndex(
          (row) => row.id === data.invoice.id
        );
        if (index >= 0 && index < this.invoices.length - 1) {
          // Confirming is a “done with this one” action: move to the next
          // invoice so a whole batch can be checked without touching the mouse.
          await this.stepPreview(1);
        }
      } catch (error) {
        /* call() already surfaced the message */
      } finally {
        this.previewSaving = false;
      }
    },

    async stepPreview(delta) {
      if (!this.preview) return;
      const rows = this.reviewQueue || this.invoices;
      if (!rows.length) return;
      let index = rows.findIndex((row) => row.id === this.preview.invoice.id);
      if (index < 0) index = delta > 0 ? -1 : rows.length;
      const next = rows[index + delta];
      if (!next) {
        this.notify(
          delta > 0 ? '已经是最后一张发票。' : '已经是第一张发票。',
          'info',
          1600
        );
        return;
      }
      await this.openPreview(next);
      this.$nextTick(() => {
        const active = document.querySelector('.invoice-table tbody tr.active');
        if (active) active.scrollIntoView({ block: 'nearest' });
      });
    },

    closePreview() {
      if (this.previewDirty && !window.confirm('有未保存的修正，确定关闭？')) return;
      const resume = this.reviewResume;
      this.preview = null;
      this.previewForm = null;
      this.invoiceEdits = [];
      this.reviewQueue = null;
      this.reviewConfirmed = [];
      this.reviewResume = false;
      if (resume) this.resumeWizard();
    },

    formatTime(value) {
      if (!value) return '';
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return value;
      return parsed.toLocaleString('zh-CN', { hour12: false });
    },

    displayText(value, limit) {
      const max = limit || 32;
      // Seller names are parsed PDF text and untrusted in confirmation
      // dialogs: strip control/format characters (incl. bidi overrides and
      // zero-width marks), collapse whitespace, then bound by code points so
      // a surrogate pair is never split.
      const text = String(value == null ? '' : value)
        .replace(/[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/gu, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      if (!text) return '（未填写）';
      const chars = Array.from(text);
      return chars.length > max
        ? `${chars.slice(0, max - 1).join('')}…`
        : text;
    },

    async reloadPreview(row) {
      const invoice = row || (this.preview && this.preview.invoice);
      if (!invoice) return;
      this.preview = {
        invoice,
        data_url: '',
        pageLabel: `${invoice.page_index + 1}/${invoice.page_count || 1}`,
        loading: true,
        error: '',
      };
      try {
        const data = await this.silentCall(
          'invoice_preview',
          invoice.id,
          this.previewZoom
        );
        if (!this.preview || this.preview.invoice.id !== invoice.id) return;
        this.preview = {
          invoice,
          data_url: data.data_url,
          pageLabel: data.page_label,
          loading: false,
          error: '',
        };
      } catch (error) {
        if (!this.preview || this.preview.invoice.id !== invoice.id) return;
        this.preview = {
          invoice,
          data_url: '',
          pageLabel: this.preview.pageLabel,
          loading: false,
          error: error.message,
        };
      }
    },

    async changeZoom(delta) {
      const next = Math.min(
        2.5,
        Math.max(0.75, Math.round((this.previewZoom + delta) * 100) / 100)
      );
      if (next === this.previewZoom) return;
      this.previewZoom = next;
      if (this.preview) await this.reloadPreview();
    },

    async linkPreviewToActiveLine() {
      if (!this.preview || !this.activeLine) return;
      await this.linkInvoices(this.activeLine, [this.preview.invoice.id]);
    },

    async addPreviewInvoice() {
      if (!this.preview) return;
      this.busy = '正在生成报销行…';
      try {
        const data = await this.call(
          'add_lines_from_invoices',
          Number(this.currentReportId),
          [this.preview.invoice.id],
          this.quickProject
        );
        await this.loadReport();
        await this.refreshInvoices(false);
        this.notify(
          data.added ? '已生成新报销行。' : '这张发票已经关联过，未重复生成。',
          data.added ? 'success' : 'error'
        );
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
    },

    /* ---------------------------------------------------------- selects */

    toggleSelectAll(event) {
      this.selected = event.target.checked ? this.invoices.map((row) => row.id) : [];
    },
  },
}).mount('#app');
