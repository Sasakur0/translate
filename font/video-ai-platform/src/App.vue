<template>
  <div class="video-gen-container">
    <el-card class="main-card" shadow="hover">
      <div class="page-content">
        <div class="workspace">
          <div class="left-pane">
            <div class="left-header">
              <h2>🎬 转换平台</h2>
            </div>
            <div class="task-pane">
              <div class="task-pane-title">任务列表</div>
              <div v-if="taskList.length === 0" class="task-empty">暂无任务</div>
              <div v-for="task in taskList" :key="task.taskId" class="task-item">
                <div class="task-head">
                  <span>{{ task.stage || '处理中' }}</span>
                  <span>{{ task.progress }}%</span>
                </div>
                <el-progress :percentage="task.progress" :stroke-width="10" />
                <div class="task-id">任务ID: {{ task.taskId }}</div>
              </div>
            </div>

            <div class="input-pane">
              <el-form :model="projectData" label-position="top">
                <el-row :gutter="24">
                  <el-col :span="8">
                    <el-form-item label="任务标题">
                      <el-input v-model="projectData.title" placeholder="例如：会议总结" />
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item label="生成模式">
                      <el-select v-model="projectData.type" placeholder="请选择" style="width: 100%">
                        <el-option label="📝 智能摘要" value="summary" />
                        <el-option label="✂️ 会议记录" value="clip" />
                      </el-select>
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item label="执行模式">
                      <el-select v-model="projectData.engine" placeholder="请选择" style="width: 100%">
                        <el-option label="本地 (Whisper Turbo)" value="local" />
                        <el-option label="千问听悟 (OSS)" value="tingwu" />
                      </el-select>
                    </el-form-item>
                  </el-col>
                </el-row>
              </el-form>
            </div>

            <div class="chat-input">
              <div class="input-area">
                <el-input
                  v-model="projectData.url"
                  placeholder="https://www.bilibili.com/video/..."
                  size="large"
                  clearable
                >
                  <template #prepend>
                    <el-icon><Link /></el-icon>
                  </template>
                </el-input>
              </div>
              <div class="prompt-area">
                <el-input
                  v-model="projectData.prompt"
                  type="textarea"
                  :rows="3"
                  placeholder="输入你希望生成的内容方向，例如：总结重点、输出章节目录、提炼金句…"
                  resize="none"
                />
                <div class="action-row">
                  <el-button type="danger" plain size="large" :disabled="!loading || !currentTaskId" @click="handleCancel">
                    取消任务
                  </el-button>
                  <el-button type="primary" size="large" :loading="loading" @click="handleGenerate">
                    开始生成
                  </el-button>
                </div>
              </div>
            </div>
          </div>

          <div class="right-pane">
            <div class="result-pane">
              <div v-if="loading" class="loading-box">
                <h3 class="status-text">任务执行中</h3>
                <p class="sub-text">进度请查看左侧任务列表</p>
              </div>

              <div v-else-if="generatedResult" class="result-box">
                <el-alert title="生成成功" type="success" show-icon :closable="false" class="mb-4"/>
                
                <div class="result-display">
                  <div class="result-header">
                    <span>生成内容预览</span>
                    <el-tag type="success">耗时 {{ generatedResult.processTime }}s</el-tag>
                  </div>
                  <el-input
                    v-model="generatedResult.content"
                    type="textarea"
                    :rows="20"
                    class="result-textarea"
                  />
                </div>
              </div>

              <div v-else class="empty-result">
              </div>
            </div>
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue';
import { ElMessage } from 'element-plus';
import { Link } from '@element-plus/icons-vue';
import axios from 'axios';

// 状态定义
const loading = ref(false);
const loadingPercent = ref(0);
const loadingStatusText = ref('初始化连接...');
const generatedResult = ref(null);
const taskList = ref([]);
const currentTaskId = ref('');

// 数据模型
const projectData = reactive({
  url: '',
  prompt: '',
  title: '',
  type: 'summary',
  engine: 'local',
  timeRange: [0, 120],
  options: {
    enableOCR: false,
    enhanceAudio: true
  }
});

// 进度条颜色配置
const colors = [
  { color: '#f56c6c', percentage: 20 },
  { color: '#e6a23c', percentage: 40 },
  { color: '#5cb87a', percentage: 100 },
];

// --- 方法 ---

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const upsertTask = (patch) => {
  const idx = taskList.value.findIndex((item) => item.taskId === patch.taskId);
  if (idx === -1) {
    taskList.value.unshift({
      taskId: patch.taskId,
      progress: patch.progress ?? 0,
      stage: patch.stage ?? '任务创建中',
      status: patch.status ?? 'PENDING'
    });
    return;
  }
  taskList.value[idx] = { ...taskList.value[idx], ...patch };
};

const pollGenerateTask = async (taskId) => {
  const maxPollTimes = 600; // 最长约 20 分钟（每次 2s）
  for (let i = 0; i < maxPollTimes; i += 1) {
    const { data } = await axios.get(`${API_BASE}/api/generate/${taskId}`, { timeout: 15000 });
    const status = (data?.status || '').toUpperCase();
    const progress = Number(data?.progress ?? 0);
    const stage = data?.stage || '';
    upsertTask({ taskId, progress, stage, status });

    loadingPercent.value = Math.max(0, Math.min(100, progress));
    loadingStatusText.value = stage || '处理中...';

    if (status === 'PENDING') {
      await sleep(2000);
      continue;
    }
    if (status === 'RUNNING') {
      await sleep(2000);
      continue;
    }
    if (status === 'SUCCESS') {
      loadingPercent.value = 100;
      loadingStatusText.value = '处理完成';
      upsertTask({ taskId, progress: 100, stage: '处理完成', status });
      return data;
    }
    if (status === 'FAILED') {
      upsertTask({ taskId, progress: 100, stage: '处理失败', status });
      throw new Error(data?.detail || '任务执行失败');
    }
    if (status === 'CANCELED') {
      upsertTask({ taskId, progress: 100, stage: '任务已取消', status });
      throw new Error(data?.detail || '任务已取消');
    }

    await sleep(2000);
  }
  throw new Error('任务轮询超时，请稍后重试');
};

const handleCancel = async () => {
  if (!currentTaskId.value) return;
  try {
    await axios.post(`${API_BASE}/api/generate/${currentTaskId.value}/cancel`, {}, { timeout: 10000 });
    ElMessage.success('已发送取消请求');
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err?.message || '取消失败');
  }
};

// 核心：对接后端
const handleGenerate = async () => {
  if (!projectData.url) return ElMessage.warning('请先输入视频链接');
  if (!projectData.prompt) return ElMessage.warning('请先输入 prompt');
  loading.value = true;
  loadingPercent.value = 0;
  generatedResult.value = null;

  // 1. 准备发给后端的数据
  const payload = {
    video_url: projectData.url,
    params: {
      ...projectData,
      timestamp: new Date().getTime()
    }
  };

  console.log('>>> 发送给后端的数据:', JSON.stringify(payload, null, 2));

  const startTime = performance.now();
  try {
    loadingStatusText.value = '任务创建中...';
    const createRes = await axios.post(`${API_BASE}/api/generate`, payload, { timeout: 30000 });
    const taskId = createRes.data?.taskId;
    if (!taskId) {
      throw new Error('后端未返回 taskId');
    }
    currentTaskId.value = taskId;
    upsertTask({ taskId, progress: 0, stage: '任务创建成功', status: 'PENDING' });

    const result = await pollGenerateTask(taskId);

    const duration = (performance.now() - startTime) / 1000;
    generatedResult.value = {
      code: result?.code ?? 200,
      processTime: result?.processTime ?? Number(duration.toFixed(1)),
      content: result?.content ?? ''
    };
  } catch (err) {
    loadingStatusText.value = '请求失败';
    ElMessage.error(err?.response?.data?.detail || err?.message || '请求出错');
    console.error(err);
  } finally {
    loading.value = false;
    currentTaskId.value = '';
  }
};

const resetFlow = () => {
  projectData.url = '';
  projectData.prompt = '';
  generatedResult.value = null;
};
</script>

<style scoped>
/* 容器布局 */
.video-gen-container {
  width: 100%;
  height: 100vh;
  margin: 0;
  padding: 10px 12px;
  box-sizing: border-box;
  overflow: hidden;
}

.main-card {
  height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  border-radius: 10px;
  overflow: hidden;
}

.main-card :deep(.el-card__body) {
  height: 100%;
  padding: 10px;
  box-sizing: border-box;
  overflow: hidden;
}

.page-content {
  padding: 0;
  height: 100%;
  min-height: 0;
  box-sizing: border-box;
  overflow: hidden;
}

.workspace {
  display: flex;
  align-items: stretch;
  gap: 16px;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.left-pane {
  flex: 0 0 30%;
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 320px;
  max-height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
}

.right-pane {
  flex: 1;
  min-width: 0;
  max-height: 100%;
  overflow: hidden;
}

.left-header {
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 12px;
  background: #fff;
}

.left-header h2 {
  margin: 0;
  font-size: 20px;
  color: #303133;
}

.task-pane {
  min-height: 280px;
  border: 1px solid #ebeef5;
  border-radius: 12px;
  background: #fff;
  padding: 14px;
}

.task-pane-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  color: #303133;
}

.task-empty {
  color: #909399;
  font-size: 13px;
}

.task-item {
  padding: 10px;
  border: 1px solid #f0f2f5;
  border-radius: 10px;
  background: #fafafa;
  margin-bottom: 10px;
}

.task-head {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: #606266;
  margin-bottom: 8px;
}

.task-id {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
  word-break: break-all;
}

.result-pane {
  height: 100%;
  max-height: 100%;
  min-height: 0;
  padding: 20px;
  border: 1px solid #ebeef5;
  border-radius: 12px;
  background: #ffffff;
  overflow-y: auto;
  overflow-x: hidden;
  box-sizing: border-box;
}

.empty-result {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 220px;
  color: #909399;
}

.empty-text {
  margin-top: 12px;
  font-size: 14px;
}

.input-pane {
  padding: 12px 6px;
}

.advanced-options {
  margin-top: 30px;
  padding-top: 20px;
  border-top: 1px dashed #dcdfe6;
}

.option-title {
  font-size: 14px;
  color: #909399;
  margin-bottom: 15px;
}

.chat-input {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px;
  border-radius: 12px;
  background: #f8fafc;
  border: 1px solid #e4e7ed;
}

.input-area {
  width: 100%;
}

.prompt-area {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.action-row {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

/* 结果区 */
.loading-box {
  text-align: center;
  padding-top: 50px;
}

.status-text {
  margin-top: 20px;
  color: #409EFF;
}

.sub-text {
  color: #909399;
  font-size: 14px;
}

.result-display {
  margin-top: 20px;
}

.result-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
  font-size: 14px;
  color: #606266;
}

/* 动画 */
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.mb-4 { margin-bottom: 2rem; }
</style>