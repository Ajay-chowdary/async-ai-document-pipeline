(() => {
  const metricsRoot = document.getElementById("metrics");
  const jobsTableBody = document.querySelector("#jobs-table tbody");
  const jobsUpdated = document.getElementById("jobs-updated");
  const uploadForm = document.getElementById("upload-form");
  const uploadStatus = document.getElementById("upload-status");
  const retryButton = document.getElementById("retry-button");
  const retryStatus = document.getElementById("retry-status");

  const pollIntervalMs = metricsRoot
    ? Number(metricsRoot.dataset.pollIntervalMs || 3000)
    : 3000;
  const recentLimit = metricsRoot
    ? Number(metricsRoot.dataset.recentLimit || 25)
    : 25;

  function formatRate(value) {
    if (value === null || value === undefined) return "—";
    return `${Math.round(Number(value) * 1000) / 10}%`;
  }

  function formatMetric(key, value) {
    if (value === null || value === undefined) return "—";
    if (key === "approximate_success_rate") return formatRate(value);
    return String(value);
  }

  async function refreshMetrics() {
    if (!metricsRoot) return;
    const response = await fetch("/metrics-summary");
    if (!response.ok) throw new Error(`metrics ${response.status}`);
    const data = await response.json();
    metricsRoot.querySelectorAll("[data-key]").forEach((node) => {
      const key = node.getAttribute("data-key");
      node.textContent = formatMetric(key, data[key]);
    });
  }

  function renderJobs(items) {
    if (!jobsTableBody) return;
    if (!items.length) {
      jobsTableBody.innerHTML =
        '<tr><td colspan="6" class="empty">No jobs yet. Upload a document to start.</td></tr>';
      return;
    }
    jobsTableBody.innerHTML = items
      .map((job) => {
        const type = job.detected_document_type || "—";
        const duration =
          job.processing_duration_ms === null ||
          job.processing_duration_ms === undefined
            ? "—"
            : `${job.processing_duration_ms} ms`;
        const error = job.error_message
          ? `<span class="error-cell">${escapeHtml(job.error_message)}</span>`
          : "—";
        const retry =
          job.status === "failed"
            ? `<button type="button" class="btn" data-retry-id="${job.id}">Retry</button>`
            : "";
        return `<tr>
          <td><span class="badge status-${job.status}">${job.status}</span></td>
          <td><a href="/jobs/${job.id}">${escapeHtml(job.original_filename || job.id)}</a></td>
          <td class="mono">${escapeHtml(type)}</td>
          <td class="mono">${duration}</td>
          <td>${error}</td>
          <td>${retry}</td>
        </tr>`;
      })
      .join("");
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  async function refreshJobs() {
    if (!jobsTableBody) return;
    const response = await fetch(`/api/v1/jobs?limit=${recentLimit}&offset=0`);
    if (!response.ok) throw new Error(`jobs ${response.status}`);
    const data = await response.json();
    renderJobs(data.items || []);
    if (jobsUpdated) {
      jobsUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }
  }

  async function poll() {
    try {
      await Promise.all([refreshMetrics(), refreshJobs()]);
    } catch (error) {
      if (jobsUpdated) {
        jobsUpdated.textContent = `Refresh failed: ${error.message}`;
      }
    }
  }

  async function retryJob(jobId, statusNode) {
    if (statusNode) statusNode.textContent = "Re-queuing…";
    const response = await fetch(`/api/v1/jobs/${jobId}/retry`, { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const message =
        (body.error && body.error.message) || `Retry failed (${response.status})`;
      if (statusNode) statusNode.textContent = message;
      return;
    }
    if (statusNode) statusNode.textContent = "Queued again.";
    await poll();
    if (!jobsTableBody) {
      window.setTimeout(() => window.location.reload(), 400);
    }
  }

  if (uploadForm) {
    uploadForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = uploadForm.querySelector('button[type="submit"]');
      const formData = new FormData(uploadForm);
      if (!formData.get("document_type")) {
        formData.delete("document_type");
      }
      if (submit) submit.disabled = true;
      if (uploadStatus) uploadStatus.textContent = "Uploading…";
      try {
        const response = await fetch("/api/v1/documents", {
          method: "POST",
          body: formData,
        });
        const body = await response.json();
        if (!response.ok) {
          throw new Error(
            (body.error && body.error.message) || `Upload failed (${response.status})`
          );
        }
        if (uploadStatus) {
          uploadStatus.textContent = `Queued job ${body.job_id}`;
        }
        uploadForm.reset();
        await poll();
      } catch (error) {
        if (uploadStatus) uploadStatus.textContent = error.message;
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  }

  if (jobsTableBody) {
    jobsTableBody.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const jobId = target.getAttribute("data-retry-id");
      if (!jobId) return;
      retryJob(jobId, jobsUpdated);
    });
  }

  if (retryButton) {
    retryButton.addEventListener("click", () => {
      retryJob(retryButton.dataset.jobId, retryStatus);
    });
  }

  if (metricsRoot || jobsTableBody) {
    poll();
    window.setInterval(poll, pollIntervalMs);
  }
})();
