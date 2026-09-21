<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Sortable from 'sortablejs'
import * as api from './api'

const limits = ref({ max_files: 50, max_file_mb: 200, max_total_mb: 1024 })

const jobId = ref(null)
// files 的**数组顺序就是合并顺序** —— 拖拽直接改这个数组，不再存一份 order。
// 一份数据只有一个顺序，就不会出现「显示的顺序」和「提交的顺序」不一致。
const files = ref([])

const status = ref('idle') // idle | ready | working | done | failed
const progress = ref({ done: 0, total: 0, message: '' })
const errorText = ref('')
const uploading = ref(false)
const uploadPct = ref(0)
const isOver = ref(false)
const busy = ref(false)
const forceAll = ref(false)
const brokenThumbs = ref([])
const outputName = ref('')

const ledgerEl = ref(null)
let sortable = null
let poller = null
let dragDepth = 0

const fileInput = ref(null)

// ------------------------------------------------------------------ 计算
const totalPages = computed(() =>
  files.value.reduce((sum, f) => sum + (f.pages || 0), 0),
)
const totalBytes = computed(() => files.value.reduce((sum, f) => sum + f.size, 0))
const signedCount = computed(() => files.value.filter((f) => f.signed).length)
const canMerge = computed(
  () => files.value.length > 0 && status.value === 'ready' && !busy.value,
)

const sizeText = (bytes) => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
}

// ------------------------------------------------------------------ 上传
function defaultOutputName() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `merged_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(
    d.getHours(),
  )}${p(d.getMinutes())}${p(d.getSeconds())}`
}

async function send(list) {
  // 只看扩展名不够 —— 后端会验魔数，这里挡一道是为了不让用户白等一次上传。
  const incoming = Array.from(list)
  if (!incoming.length) return

  errorText.value = ''
  uploading.value = true
  uploadPct.value = 0
  try {
    const onProgress = (p) => (uploadPct.value = p)
    const res = jobId.value
      ? await api.addFiles(jobId.value, incoming, onProgress)
      : await api.createJob(incoming, onProgress)
    jobId.value = res.job_id
    files.value = res.files
    if (res.limits) limits.value = res.limits
    if (!outputName.value) outputName.value = defaultOutputName()
    status.value = 'ready'
    brokenThumbs.value = []
    await nextTick()
    bindSortable()
  } catch (err) {
    errorText.value = err.message
  } finally {
    uploading.value = false
    uploadPct.value = 0
  }
}

const onPick = (e) => {
  send(e.target.files)
  e.target.value = '' // 允许连续两次选同一个文件
}

// 拖拽用计数器而不是布尔量：拖过子元素时会连续触发 dragleave，
// 用布尔量会让高亮状态疯狂闪烁。
const onDragEnter = (e) => {
  if (!e.dataTransfer?.types?.includes('Files')) return
  dragDepth += 1
  isOver.value = true
}
const onDragLeave = () => {
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) isOver.value = false
}
const onDrop = (e) => {
  dragDepth = 0
  isOver.value = false
  if (status.value === 'working') return
  send(e.dataTransfer.files)
}

// 从邮件、微信里拿到 PDF 之后直接 Ctrl+V，比另存为再选文件少两步。
const onPaste = (e) => {
  if (!e.clipboardData?.files?.length) return
  if (status.value === 'working') return
  send(e.clipboardData.files)
}

// ------------------------------------------------------------------ 清单
function bindSortable() {
  if (sortable || !ledgerEl.value) return
  sortable = Sortable.create(ledgerEl.value, {
    handle: '.handle',
    animation: 120,
    ghostClass: 'ghost',
    chosenClass: 'chosen',
    onEnd: ({ oldIndex, newIndex }) => {
      if (oldIndex === newIndex) return
      const next = files.value.slice()
      next.splice(newIndex, 0, next.splice(oldIndex, 1)[0])
      files.value = next
    },
  })
}

const removeAt = async (id) => {
  if (status.value !== 'ready') return
  try {
    const res = await api.removeFile(jobId.value, id)
    files.value = res.files
  } catch (err) {
    errorText.value = err.message
  }
}

function sortByName() {
  if (status.value !== 'ready') return
  // 用 localeCompare + zh 让中文按拼音排序，符合中文用户对「按名字排」的预期。
  files.value = files.value
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, 'zh'))
}

const markBroken = (id) => {
  if (!brokenThumbs.value.includes(id)) brokenThumbs.value.push(id)
}

// ------------------------------------------------------------------ 合并
async function merge() {
  if (!canMerge.value) return
  busy.value = true
  errorText.value = ''
  try {
    const keys = files.value.map((f) => f.id)
    await api.startMerge(jobId.value, {
      order: keys,
      force_flatten: forceAll.value ? keys : [],
      output_name: outputName.value,
    })
    status.value = 'working'
    progress.value = { done: 0, total: files.value.length, message: '排队中…' }
    startPolling()
  } catch (err) {
    errorText.value = err.message
  } finally {
    busy.value = false
  }
}

function startPolling() {
  stopPolling()
  poller = setInterval(refresh, 800)
}

function stopPolling() {
  if (poller) {
    clearInterval(poller)
    poller = null
  }
}

async function refresh() {
  if (!jobId.value) return
  try {
    const res = await api.getJob(jobId.value)
    // 合并开始后清单就锁定了，服务端返回的文件顺序与本地一致（服务端按上传
    // 顺序存，我们按本地顺序显示）—— 所以这里只更新每一条的状态，不动数组顺序。
    for (const remote of res.files) {
      const local = files.value.find((f) => f.id === remote.id)
      if (local) {
        local.action = remote.action
        if (remote.error) local.error = remote.error
      }
    }
    progress.value = res.progress

    if (res.status === 'done') {
      status.value = 'done'
      stopPolling()
    } else if (res.status === 'failed') {
      status.value = 'failed'
      errorText.value = res.error || '合并失败。'
      stopPolling()
    }
  } catch (err) {
    stopPolling()
    errorText.value = err.message
  }
}

function download() {
  const a = document.createElement('a')
  a.href = api.downloadUrl(jobId.value)
  a.download = outputName.value ? `${outputName.value}.pdf` : 'merged.pdf'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

function reset() {
  stopPolling()
  jobId.value = null
  files.value = []
  status.value = 'idle'
  progress.value = { done: 0, total: 0, message: '' }
  errorText.value = ''
  outputName.value = ''
  forceAll.value = false
  brokenThumbs.value = []
  sortable?.destroy()
  sortable = null
}

// ------------------------------------------------------------------ 生命周期
onMounted(async () => {
  document.addEventListener('paste', onPaste)
  try {
    limits.value = await api.getLimits()
  } catch {
    /* 拿不到限制就用默认值，不阻塞使用 */
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('paste', onPaste)
  stopPolling()
  sortable?.destroy()
})
</script>

<template>
  <div class="shell">
    <header class="masthead">
      <h1>PDF 合并</h1>
      <span class="tagline">按顺序合并成一个 PDF</span>
      <span class="limits">
        ≤ {{ limits.max_files }} 个 · 单文件 ≤ {{ limits.max_file_mb }} MB ·
        合计 ≤ {{ limits.max_total_mb }} MB
      </span>
    </header>

    <!-- 投放区：有文件之后收窄成一条「继续添加」的横条 -->
    <div
      class="drop"
      :class="{ 'is-over': isOver, busy: uploading }"
      @click="fileInput?.click()"
      @dragenter.prevent="onDragEnter"
      @dragover.prevent
      @dragleave.prevent="onDragLeave"
      @drop.prevent="onDrop"
    >
      <input
        ref="fileInput"
        type="file"
        accept="application/pdf,.pdf"
        multiple
        hidden
        @change="onPick"
      />
      <div class="headline">
        {{ files.length ? '继续添加 PDF' : '把 PDF 拖到这里' }}
      </div>
      <div class="hint">
        或点击选择文件，也可以直接 <kbd>Ctrl</kbd> + <kbd>V</kbd> 粘贴
      </div>
      <div
        v-if="uploading"
        class="upload-bar"
        :style="{ transform: `scaleX(${uploadPct || 0.02})` }"
      ></div>
    </div>

    <div v-if="errorText" class="notice">
      <span class="label">错误</span>
      <span>{{ errorText }}</span>
    </div>

    <!-- 清单 -->
    <template v-if="files.length">
      <div class="toolbar">
        <span class="counts">
          {{ files.length }} 个文件 · {{ totalPages }} 页 ·
          {{ sizeText(totalBytes) }}
          <template v-if="signedCount"> · {{ signedCount }} 个含签名</template>
        </span>
        <span class="spacer"></span>
        <button
          v-if="status === 'ready'"
          class="linkish"
          type="button"
          @click="sortByName"
        >
          按文件名排序
        </button>
        <button
          v-if="status === 'ready'"
          class="linkish"
          type="button"
          @click="reset"
        >
          全部清空
        </button>
      </div>

      <div ref="ledgerEl" class="ledger">
        <div
          v-for="(f, i) in files"
          :key="f.id"
          class="row"
          :class="{ 'is-working': f.action || f.error }"
        >
          <span v-if="status === 'ready'" class="handle" title="拖动调整顺序">⠿</span>
          <span v-else class="handle" style="visibility: hidden">⠿</span>

          <span class="ordinal">{{ i + 1 }}</span>

          <div class="thumb">
            <img
              v-if="!brokenThumbs.includes(f.id) && jobId"
              :src="api.thumbUrl(jobId, f.id)"
              alt=""
              loading="lazy"
              @error="markBroken(f.id)"
            />
            <span v-else class="placeholder">PDF</span>
          </div>

          <div class="name">
            {{ f.name }}
            <span v-if="f.error" class="sub warn">{{ f.error }}</span>
          </div>

          <span class="metric pages">
            {{ f.pages == null ? '—' : `${f.pages} 页` }}
          </span>
          <span class="metric dim">{{ sizeText(f.size) }}</span>

          <div class="cell-end">
            <!-- 含签名的标记：它是提示不是警告，所以做得克制。
                 鼠标悬停给出「外观会保留、签名信息会失效」这句关键说明。 -->
            <span
              v-if="f.error"
              class="mark ghost"
              title="该文件无法合并"
              >不可用</span
            >
            <span
              v-else-if="f.action === 'bake' || f.action === 'raster'"
              class="mark processing"
              title="已把签名图样固化进页面"
              >已固化</span
            >
            <span
              v-else-if="f.signed"
              class="mark"
              title="检测到数字签名。合并时会保留签名图样，但签名的密码学有效性会失效。"
              >签名</span
            >
            <span v-else-if="f.action === 'direct'" class="tick" title="已合并">✓</span>
            <button
              v-if="status === 'ready'"
              class="drop-file"
              type="button"
              title="从列表中移除"
              @click.stop="removeAt(f.id)"
            >
              ×
            </button>
          </div>
        </div>
      </div>

      <!-- 进度 -->
      <div v-if="status === 'working'" class="progress">
        <div class="line">
          <span>{{ progress.message || '正在合并…' }}</span>
          <span class="pct">
            {{ progress.done }} / {{ progress.total }}
          </span>
        </div>
        <div class="track">
          <i
            :style="{
              width: progress.total
                ? `${(progress.done / progress.total) * 100}%`
                : '2%',
            }"
          ></i>
        </div>
      </div>

      <!-- 结果 -->
      <div v-if="status === 'done'" class="result">
        <div>
          <div class="headline">合并完成</div>
          <div class="meta">
            {{ files.length }} 个文件 · {{ totalPages }} 页 ·
            {{ sizeText(totalBytes) }}
          </div>
        </div>
        <span class="spacer"></span>
        <button class="primary" type="button" @click="download">下载 PDF</button>
        <button class="linkish" type="button" @click="reset">再合一次</button>
      </div>

      <!-- 操作栏 -->
      <div v-if="status === 'ready'" class="actionbar">
        <div class="field">
          <label for="out">输出文件名</label>
          <div class="input-wrap">
            <input id="out" v-model="outputName" spellcheck="false" />
            <span class="suffix">.pdf</span>
          </div>
        </div>

        <label class="check">
          <input v-model="forceAll" type="checkbox" />
          强制保留签名外观
        </label>

        <span class="spacer" style="margin-left: auto"></span>

        <button
          class="primary wide"
          type="button"
          :disabled="!canMerge"
          @click="merge"
        >
          合并 {{ files.length }} 个文件
        </button>
      </div>
    </template>

    <p v-if="files.length" class="footnote">
      拖动左侧手柄可调整文件顺序，页面将按这个顺序拼接。每个源文件在结果里会生成
      一个同名书签，文件原有的目录结构保留在其下一层。各页保持原有尺寸，不做统一。
    </p>
  </div>
</template>
