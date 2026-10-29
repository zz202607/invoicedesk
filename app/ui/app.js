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
      },
      loading: true,
      fatal: '',
      filters: {
        batch_id: '',
        search: '',
        unlinked_only: false,
        include_review: true,
        date_from: '',
        date_to: '',
      },
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
      fieldLabels: {
        invoice_number: '票号',
        invoice_date: '日期',
        seller_name: '销售方',
        buyer_name: '购买方',
        amount_total_cents: '金额',
        category: '分类',
        item_summary: '项目',
        category_rule: '分类来源',
        review: '复核',
        warning: '警告',
      },
      poolVisible: true,
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
      pickingFolder: false,
      showRulesModal: false,
      rulesLoading: false,
      categoryRules: [],
      showDashboardModal: false,
      dashboardLoading: false,
      dashboard: null,
      showShortcutsModal: false,
      themeMode: 'auto',
      busy: '',
      toast: { message: '', kind: 'info', timer: null },
      exportResult: null,
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
    modalOpen() {
      return (
        this.showReportModal ||
        this.showMileageModal ||
        this.showRulesModal ||
        this.showImportModal ||
        this.showDashboardModal ||
        this.showShortcutsModal ||
        Boolean(this.exportResult)
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
    this.syncTheme();
  },

  beforeUnmount() {
    document.removeEventListener('keydown', this.onKeydown);
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
        if (this.exportResult) this.exportResult = null;
        else if (this.showShortcutsModal) this.showShortcutsModal = false;
        else if (this.showDashboardModal) this.showDashboardModal = false;
        else if (this.showReportModal) this.showReportModal = false;
        else if (this.showMileageModal) this.showMileageModal = false;
        else if (this.showRulesModal) this.showRulesModal = false;
        else if (this.showImportModal) this.showImportModal = false;
        else if (this.preview) this.closePreview();
        return;
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

    filterPayload() {
      return {
        batch_id:
          this.filters.batch_id === '' ? null : Number(this.filters.batch_id),
        search: this.filters.search,
        unlinked_only: this.filters.unlinked_only,
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
      try {
        if (this.reportModalMode === 'create') {
          const data = await this.call('create_report', { ...form });
          this.currentReportId = data.report_id;
        } else {
          await this.call('update_report', Number(this.currentReportId), {
            ...form,
          });
        }
        this.showReportModal = false;
        await this.refreshReports();
        await this.loadReport();
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
      this.importForm = { path: '', label: '' };
      this.showImportModal = true;
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

    async submitImport() {
      if (!this.importForm.path) {
        this.notify('请先选择发票文件夹。', 'error');
        return;
      }
      this.busy = '正在导入发票，请稍候…';
      try {
        const data = await this.call(
          'import_folder',
          this.importForm.path,
          this.importForm.label
        );
        this.showImportModal = false;
        await this.reloadBootstrap();
        await this.refreshInvoices(true);
        this.notify(
          `导入完成：新增 ${data.invoices_imported} 张，` +
            `重复 ${data.invoices_duplicate}，冲突 ${data.invoices_conflict}，` +
            `待确认 ${data.needs_review}，合计 ¥${data.amount}。`,
          'success',
          9000
        );
      } catch (error) {
        /* surfaced already */
      } finally {
        this.busy = '';
      }
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
      this.busy = '正在导出证据包…';
      try {
        const data = await this.call('export_evidence', this.filterPayload());
        this.exportResult = {
          title: '证据包已导出',
          summary:
            `共 ${data.count} 张发票（待确认 ${data.review_count}），` +
            `合计 ¥${data.total}；PDF ${data.pages} 页。`,
          note: data.notice_only ? '部分票面为通知单，PDF 中已按唯一票面去重。' : '',
          kind: 'export',
          items: [
            { label: 'Excel 明细', path: data.excel_path },
            { label: '打印 PDF', path: data.pdf_path },
          ],
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

    /* ---------------------------------------------------------- preview */

    async openPreview(row) {
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
        if (data.rule) {
          const label = this.displayText(data.rule.pattern, 40);
          const category = this.displayText(data.rule.category, 20);
          message += data.rule.enabled
            ? ` 已记住：以后导入「${label}」自动归入「${category}」。`
            : ` 规则「${label}」当前已停用，本次只改这张发票；启用后才会影响新导入。`;
        }
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
      } catch (error) {
        /* call() already surfaced the message */
      } finally {
        this.previewSaving = false;
      }
    },

    closePreview() {
      if (this.previewDirty && !window.confirm('有未保存的修正，确定关闭？')) return;
      this.preview = null;
      this.previewForm = null;
      this.invoiceEdits = [];
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
