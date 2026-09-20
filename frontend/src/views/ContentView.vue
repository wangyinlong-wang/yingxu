<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { onBeforeRouteLeave, useRoute } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { ArrowDown, ArrowDownBold, ArrowUpBold, CircleCheckFilled, VideoCamera } from '@element-plus/icons-vue';
import { api } from '../api';
import { actionKey, formatBytes, formatDate, isConflict, statusLabel } from '../lib';
import { canApproveContent, cloneContent, contentFingerprint, contentPatch, contentPlatformIssues, normalizedContent, platformDescription, platformTextRules, platformTitleUnits, reorderedScenes, saveBeforeAction } from '../content-editor';
import type { Asset, Content, Preset, QualityReview, Template } from '../types';
import PageIntro from '../components/PageIntro.vue';
import StatePanel from '../components/StatePanel.vue';
import StatusTag from '../components/StatusTag.vue';
const list = ref<Content[]>([]);
const route = useRoute();
const current = ref<Content | null>(null);
const assets = ref<Asset[]>([]);
const templates = ref<Template[]>([]);
const presets = ref<Preset[]>([]);
const qualityReviews = ref<QualityReview[]>([]);
const savedFingerprint = ref('');
const loading = ref(false);
const detailLoading = ref(false);
const saving = ref(false);
const generating = ref(false);
const approving = ref(false);
const rejecting = ref(false);
const uploadLoading = ref(false);
const briefSelecting = ref(false);
const qualityChecking = ref(false);
const approvalDialog = ref(false);
const approvalNotes = ref('');
const approvalAutomatic = ref(false);
const complianceChecks = ref<Record<string, boolean>>({ source_rights_confirmed:false, no_unauthorized_material:false, claims_not_exaggerated:false, no_offplatform_diversion:false, no_unverifiable_guarantees:false, ai_disclosure_confirmed:false, platform_rules_checked:false, visuals_manually_reviewed:false });
const error = ref('');
const query = ref('');
const status = ref('');
const activeTab = ref('script');
const activeScene = ref(0);
const pollTimer = ref<number>();
const dirty = computed(() => Boolean(current.value) && savedFingerprint.value !== contentFingerprint(current.value!));
const unresolvedFacts = computed(() => current.value?.script.facts.filter(fact => !fact.verified || !fact.source_url.trim()).length ?? 0);
const platformIssues = computed(() => current.value ? contentPlatformIssues(current.value) : []);
const selectedPreset = computed(() => presets.value.find(item => item.id === current.value?.preset_id));
const briefConcepts = computed(() => current.value?.brief?.concepts ?? []);
const briefSelected = computed(() => Boolean(current.value?.brief?.selected_concept_id));
const briefBlocksScript = computed(() => briefConcepts.value.length > 0 && !briefSelected.value);
const latestQualityReview = computed(() => current.value?.latest_quality_review ?? qualityReviews.value[0] ?? null);
const readyToApprove = computed(() => current.value ? canApproveContent({ ...current.value, latest_quality_review:latestQualityReview.value }) : false);
const allComplianceChecked = computed(() => Object.values(complianceChecks.value).every(Boolean));
const supportedSceneTypes = computed(() => selectedPreset.value?.supported_scene_types ?? ['image', 'text_card', 'stat_card', 'quote_card']);
const images = computed(() => assets.value.filter(asset => asset.kind === 'image'));
const audios = computed(() => assets.value.filter(asset => asset.kind === 'audio'));
function updateListItem(content: Content) {
    const index = list.value.findIndex(item => item.id === content.id);
    if (index >= 0)
        list.value.splice(index, 1, cloneContent(content));
}
function applyRemote(content: Content, nextAssets?: Asset[]) {
    const normalized = normalizedContent(content);
    current.value = normalized;
    savedFingerprint.value = contentFingerprint(current.value);
    if (nextAssets)
        assets.value = nextAssets;
    updateListItem(normalized);
    activeScene.value = Math.min(activeScene.value, Math.max(0, normalized.scenes.length - 1));
}
async function loadList() {
    loading.value = true;
    error.value = '';
    try {
        const response = await api.contents(query.value, status.value);
        list.value = response.items;
    }
    catch (exception: any) {
        error.value = exception.message || '请检查本地服务是否已启动';
    }
    finally {
        loading.value = false;
    }
}
async function loadTemplates() {
    try {
        templates.value = (await api.templates()).items;
    }
    catch (exception: any) {
        ElMessage.warning(`无法获取模板：${exception.message}`);
    }
}
async function loadPresets() {
    try { presets.value = (await api.presets()).items; }
    catch (exception: any) { ElMessage.warning(`无法获取生产预设：${exception.message}`); }
}
async function selectRemote(id: string) {
    detailLoading.value = true;
    try {
        const [content, assetResponse] = await Promise.all([api.content(id), api.assets(id)]);
        applyRemote(content, assetResponse.items);
        try { qualityReviews.value = (await api.qualityReviews(id)).items; } catch { qualityReviews.value = []; }
        activeScene.value = 0;
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        detailLoading.value = false;
    }
}
async function chooseUnsavedAction(): Promise<'save' | 'discard' | 'stay'> {
    if (!dirty.value)
        return 'discard';
    try {
        await ElMessageBox.confirm('当前编辑尚未保存。保存后再继续，或放弃这些修改。', '存在未保存修改', { confirmButtonText: '保存并继续', cancelButtonText: '放弃修改', distinguishCancelAndClose: true, type: 'warning' });
        return 'save';
    }
    catch (reason) {
        return reason === 'cancel' ? 'discard' : 'stay';
    }
}
async function save(options: {
    silent?: boolean;
} = {}): Promise<boolean> {
    if (!current.value || !dirty.value)
        return true;
    if (saving.value)
        return false;
    saving.value = true;
    try {
        const saved = await api.patchContent(current.value.id, contentPatch(current.value));
        applyRemote(saved);
        if (!options.silent)
            ElMessage.success('草稿已保存。脚本或分镜更改会使审核失效；仅事实核验或平台文案修改可以复用当前视频，但仍需重新审核。');
        return true;
    }
    catch (exception: any) {
        if (isConflict(exception))
            ElMessage.error('内容已被其他编辑更新，请重新打开后再合并修改。');
        else
            ElMessage.error(exception.message);
        return false;
    }
    finally {
        saving.value = false;
    }
}
async function requestSelect(id: string) {
    if (current.value?.id === id || detailLoading.value)
        return;
    const choice = await chooseUnsavedAction();
    if (choice === 'stay')
        return;
    if (choice === 'save' && !await save())
        return;
    await selectRemote(id);
}
async function runAfterSave(action: () => Promise<void>) {
    return saveBeforeAction(dirty.value, () => save({ silent: true }), action);
}
function imageGenerationBlocker(sceneId?:string):string | undefined {
    const scenes = current.value?.scenes || [];
    if (!scenes.length)
        return '重绘配图前至少需要一个镜头。';
    if (scenes.some(scene => !scene.narration.trim() || !scene.audio_asset_ids.length))
        return '重绘配图前，每个镜头都需要已有旁白和音频素材。';
    if (sceneId && scenes.some(scene => scene.id !== sceneId && !scene.image_asset_id))
        return '重绘本镜头前，其他镜头都需要已有图片素材。';
    return undefined;
}
async function generate(stage: string, sceneId?:string) {
    if (!current.value || generating.value)
        return;
    if (['script', 'all'].includes(stage) && briefBlocksScript.value) {
        ElMessage.error('Production Brief 已生成，请先从三个方案中选择一个，再生成脚本。');
        return;
    }
    if (stage === 'motion') {
        const ready = current.value.scenes.length > 0 && current.value.scenes.every(scene => Boolean(scene.image_asset_id) && scene.audio_asset_ids.length > 0);
        if (!ready) {
            ElMessage.error('AI 编排动画前，每个镜头都需要已有图片和音频素材。');
            return;
        }
        generating.value = true;
        try {
            await ElMessageBox.confirm('将调用一次服务端已配置的文本模型，为每个镜头选择动画和转场，并用现有图片和音频重新合成视频。不会生成图片或声音；完成后内容需要重新审核。', 'AI 编排动画并合成', { confirmButtonText: '开始编排', cancelButtonText: '取消', type: 'warning' });
        }
        catch {
            generating.value = false;
            return;
        }
    }
    else if (stage === 'images') {
        const blocker = imageGenerationBlocker(sceneId);
        if (blocker) {
            ElMessage.error(blocker);
            return;
        }
        const target = sceneId ? current.value.scenes.find(scene => scene.id === sceneId) : undefined;
        const targetLabel = target ? `镜头 ${current.value.scenes.findIndex(scene => scene.id === target.id) + 1}（${target.caption || target.narration || '未命名镜头'}）` : `全部 ${current.value.scenes.length} 个镜头`;
        generating.value = true;
        try {
            await ElMessageBox.confirm(`将重绘${targetLabel}的配图。每个目标镜头会调用一次图像模型，画面提示词会结合实际旁白和来源事实；不会生成旁白或调用文本模型。完成后会重新合成视频，内容需要重新审核。`, sceneId ? '重绘本镜头配图' : '重绘配图并合成', { confirmButtonText: '开始重绘', cancelButtonText: '取消', type: 'warning' });
        }
        catch {
            generating.value = false;
            return;
        }
    }
    else {
        generating.value = true;
    }
    try {
        const contentId = current.value.id;
        const submitted = await runAfterSave(async () => {
            await api.generate(contentId, stage, actionKey(), sceneId);
        });
        if (submitted) {
            ElMessage.success('生成任务已提交。编辑器会在没有未保存修改时自动刷新当前视频和状态。');
            await pollCurrent();
        }
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        generating.value = false;
        await pollCurrent();
    }
}
async function selectBrief(conceptId:string) {
    if (!current.value || briefSelecting.value || briefSelected.value) return;
    briefSelecting.value = true;
    try {
        const contentId = current.value.id;
        let selected:Content | undefined;
        const submitted = await runAfterSave(async () => {
            if (!current.value) return;
            selected = await api.selectBrief(contentId, { version:current.value.version, concept_id:conceptId }, actionKey());
        });
        if (submitted && selected) {
            applyRemote(selected);
            qualityReviews.value = [];
            ElMessage.success('已选择创意方案。旧脚本、分镜、成片和审核已按新版本失效，请继续生成脚本。');
        }
    } catch (exception:any) {
        ElMessage.error(isConflict(exception) ? '内容版本已变化，请重新打开后选择方案。' : exception.message);
    } finally { briefSelecting.value = false; }
}
async function checkQuality() {
    if (!current.value || !current.value.video_asset_id || qualityChecking.value) return;
    qualityChecking.value = true;
    try { await api.runQualityReview(current.value.id, actionKey()); ElMessage.success('质量复检任务已提交。'); await pollCurrent(); }
    catch (exception:any) { ElMessage.error(exception.message); }
    finally { qualityChecking.value = false; }
}
async function openApproval() {
    if (!readyToApprove.value || approving.value) return;
    approving.value = true;
    try {
        const publishingSettings = await api.settings();
        approvalAutomatic.value = Boolean(publishingSettings.publishing?.auto_publish_after_approval);
        approvalNotes.value = '';
        complianceChecks.value = Object.fromEntries(Object.keys(complianceChecks.value).map(key => [key, false]));
        approvalDialog.value = true;
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        approving.value = false;
    }
}
async function submitApproval() {
    if (!current.value || approving.value || !allComplianceChecked.value) return;
    approving.value = true;
    try {
        const contentId = current.value.id;
        const submitted = await runAfterSave(async () => {
            if (!current.value || !canApproveContent({ ...current.value, latest_quality_review:latestQualityReview.value })) throw new Error('当前版本尚未满足审核条件');
            await api.approve(contentId, { version: current.value.version, notes: approvalNotes.value, auto_publish_expected: approvalAutomatic.value, compliance_confirmed: true, compliance_checks: complianceChecks.value }, actionKey());
        });
        if (submitted) { approvalDialog.value = false; await pollCurrent(); ElMessage.success('已提交审核通过'); }
    } catch (exception:any) { ElMessage.error(exception.message); }
    finally { approving.value = false; await pollCurrent(); }
}
async function approve() {
    await openApproval();
}
async function reject() {
    if (!current.value || rejecting.value)
        return;
    rejecting.value = true;
    try {
        const contentId = current.value.id;
        const submitted = await runAfterSave(async () => {
            const prompt = await ElMessageBox.prompt('请说明需要修改的内容', '驳回内容', { inputPattern: /.+/, inputErrorMessage: '驳回说明不能为空' }).catch(() => null);
            if (!prompt)
                throw new Error('已取消驳回');
            if (!current.value)
                return;
            await api.reject(contentId, { version: current.value.version, notes: prompt.value }, actionKey());
        });
        if (submitted)
            await pollCurrent();
    }
    catch (exception: any) {
        if (exception.message !== '已取消驳回')
            ElMessage.error(exception.message);
    }
    finally {
        rejecting.value = false;
        await pollCurrent();
    }
}
async function upload(file: any) {
    if (!current.value || uploadLoading.value)
        return;
    uploadLoading.value = true;
    try {
        const form = new FormData();
        form.append('file', file.raw);
        form.append('kind', file.raw.type.startsWith('audio/') ? 'audio' : 'image');
        form.append('content_id', current.value.id);
        assets.value.push(await api.uploadAsset(form));
        ElMessage.success('素材已上传，请在分镜中选择后保存草稿。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        uploadLoading.value = false;
    }
}
function addFact() {
    current.value?.script.facts.push({ claim: '', source_url: '', verified: false, notes: '' });
}
function addScene() {
    if (!current.value)
        return;
    current.value.scenes.push({ id: crypto.randomUUID(), order: current.value.scenes.length + 1, narration: '', caption: '', image_prompt: '', image_asset_id: null, audio_asset_ids: [], motion: 'auto', transition: 'fade', scene_type:'image', visual_data:{headline:'',body:'',value:'',source_url:'',emphasis:''}, hero_moment:false, shot_intent:'' });
    activeScene.value = current.value.scenes.length - 1;
}
function removeScene(index: number) {
    if (!current.value)
        return;
    current.value.scenes = current.value.scenes.filter((_, sceneIndex) => sceneIndex !== index).map((scene, sceneIndex) => ({ ...scene, order: sceneIndex + 1 }));
    activeScene.value = Math.max(0, index - 1);
}
function moveScene(index: number, delta: number) {
    if (!current.value)
        return;
    const next = index + delta;
    if (next < 0 || next >= current.value.scenes.length)
        return;
    current.value.scenes = reorderedScenes(current.value.scenes, index, next);
    activeScene.value = next;
}
function clearImageAsset() {
    const scene = current.value?.scenes[activeScene.value];
    if (scene)
        scene.image_asset_id = null;
}
async function pollCurrent() {
    if (!current.value || dirty.value || detailLoading.value || saving.value || generating.value || approving.value || rejecting.value || uploadLoading.value)
        return;
    const contentId = current.value.id;
    try {
        const [remote, assetResponse] = await Promise.all([api.content(contentId), api.assets(contentId)]);
        if (!dirty.value && current.value?.id === contentId)
            applyRemote(remote, assetResponse.items);
        try { qualityReviews.value = (await api.qualityReviews(contentId)).items; } catch { qualityReviews.value = []; }
    }
    catch {
        // Background polling must not replace a usable editor with transient connection errors.
    }
}
onBeforeRouteLeave(async () => {
    const choice = await chooseUnsavedAction();
    if (choice === 'stay')
        return false;
    return choice !== 'save' || await save();
});
onMounted(async () => {
    await Promise.all([loadList(), loadTemplates(), loadPresets()]);
    if (typeof route.query.content_id === 'string')
        await selectRemote(route.query.content_id);
    pollTimer.value = window.setInterval(pollCurrent, 5000);
});
onBeforeUnmount(() => { if (pollTimer.value)
    clearInterval(pollTimer.value); });
</script>

<template>
<PageIntro title="内容工作台" description="编辑脚本与镜头，上传并选择素材；提交生成后，必须完成事实复核和视频审核。">
  <el-button @click="loadList">刷新列表</el-button>
</PageIntro>
<div class="workspace">
  <aside class="master">
    <div class="filter-row">
      <el-input v-model="query" placeholder="搜索内容" clearable @change="loadList" />
      <el-select
        v-model="status"
        placeholder="全部状态"
        clearable
        @change="loadList"
      >
        <el-option
          v-for="value in ['DRAFT','GENERATING','REVIEW_REQUIRED','APPROVED','REJECTED']"
          :key="value"
          :value="value"
          :label="value"
        />
      </el-select>
    </div>
    <StatePanel
      :loading
      :error
      :empty="!list.length"
      empty-text="暂无内容。请到选题库创建内容草稿。"
      @retry="loadList"
    >
      <button
        v-for="item in list"
        :key="item.id"
        class="content-item"
        :class="{selected:current?.id===item.id}"
        @click="requestSelect(item.id)"
      >
        <b>{{ item.title }}</b>
        <span>
          <StatusTag :status="item.status" />
          v
          {{ item.version }}
        </span>
        <small>{{ formatDate(item.updated_at) }}</small>
      </button>
    </StatePanel>
  </aside>
  <section class="detail">
    <el-empty v-if="!current && !detailLoading" description="从左侧选择一条内容开始编辑" />
    <el-skeleton v-else-if="detailLoading" :rows="8" animated />
    <template v-else-if="current">
      <div class="content-head">
        <div>
          <el-input v-model="current.title" size="large" class="title-input" />
          <p>
            版本
            {{ current.version }}
            ·
            {{ formatDate(current.updated_at) }}
            ·
            <StatusTag :status="current.status" />
            <el-tag v-if="dirty" type="warning" size="small">未保存</el-tag>
          </p>
        </div>
        <div class="actions">
          <el-button :loading="saving" :disabled="!dirty" @click="save">保存草稿</el-button>
          <el-dropdown :disabled="generating || saving" @command="(stage:string)=>generate(stage)">
            <el-button type="primary" :loading="generating">
              生成
              <el-icon class="el-icon--right">
                <ArrowDown />
              </el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="brief">生成 Production Brief（3 个方案）</el-dropdown-item>
                <el-dropdown-item command="all">生成全部</el-dropdown-item>
                <el-dropdown-item command="script">仅脚本</el-dropdown-item>
                <el-dropdown-item command="assets">仅素材</el-dropdown-item>
                <el-dropdown-item command="render">仅渲染视频</el-dropdown-item>
                <el-dropdown-item command="images">重绘配图并合成</el-dropdown-item>
                <el-dropdown-item command="motion">AI 编排动画并合成</el-dropdown-item>
                <el-dropdown-item command="local_voice">用本人声音生成视频</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
      <el-alert
        v-if="current.status==='REVIEW_REQUIRED'"
        type="warning"
        :closable="false"
        show-icon
        title="生成结果待人工复核。保存事实或平台文案后仍需要重新审核。"
      />
      <el-card class="production-brief" shadow="never">
        <template #header><div class="card-title"><span>生产预设与 Production Brief</span><el-tag v-if="current.brief?.selected_concept_id" type="success">已选择方案</el-tag></div></template>
        <el-form label-position="top"><el-form-item label="生产预设"><el-select v-model="current.preset_id" :disabled="generating || briefConcepts.length>0" placeholder="选择预设"><el-option v-for="preset in presets" :key="preset.id" :value="preset.id" :label="`${preset.name} · v${preset.version}`" /></el-select><p v-if="selectedPreset" class="form-help">{{selectedPreset.description}} · 支持 {{selectedPreset.supported_scene_types.join(' / ')}}；{{selectedPreset.quality_rules.min_visual_types}} 种视觉类型、至少一个主视觉镜头。</p><p v-else class="form-help">旧内容默认使用科技快讯预设。</p></el-form-item></el-form>
        <el-alert v-if="briefBlocksScript" type="warning" :closable="false" title="已生成 Brief，必须先选择一个方案才可生成脚本或运行完整流程。" />
        <el-empty v-if="!briefConcepts.length" description="尚未生成 Brief。可选预设后使用“生成”菜单生成 3 个可比较方案。" :image-size="45" />
        <div v-else class="brief-grid"><article v-for="concept in briefConcepts" :key="concept.id" class="brief-card" :class="{selected:current.brief?.selected_concept_id===concept.id}"><div class="card-title"><b>{{concept.title}}</b><el-tag v-if="current.brief?.selected_concept_id===concept.id" type="success">已选</el-tag></div><p><b>钩子：</b>{{concept.hook}}</p><p><b>结构：</b>{{concept.narrative_structure}} · {{concept.visual_approach}}</p><p><b>核心信息：</b>{{concept.core_message}}</p><p><b>为什么有效：</b>{{concept.why_this_works}}</p><small>预计调用：文本 {{concept.estimated_calls.text}} · 图片 {{concept.estimated_calls.image}} · 配音 {{concept.estimated_calls.speech}}<br>预计费用：{{concept.estimated_cost_usd == null ? '费用未知' : `$${concept.estimated_cost_usd}`}}</small><el-button v-if="!current.brief?.selected_concept_id" type="primary" plain :loading="briefSelecting" @click="selectBrief(concept.id)">选择此方案</el-button></article></div>
      </el-card>
      <el-tabs v-model="activeTab" class="editor-tabs">
        <el-tab-pane label="脚本与事实" name="script">
          <el-form label-position="top">
            <el-form-item label="内容角度">
              <el-input v-model="current.script.angle" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="选题理由">
              <el-input v-model="current.script.selection_reason" type="textarea" :rows="2" />
            </el-form-item>
          </el-form>
          <div class="section-title">
            <h3>事实核验</h3>
            <el-button size="small" @click="addFact">添加事实</el-button>
          </div>
          <el-alert v-if="!current.script.facts.length" type="warning" :closable="false" title="至少添加并核验一条有来源的事实，才能通过审核。" />
          <el-alert
            v-else-if="unresolvedFacts"
            type="warning"
            :closable="false"
            :title="`还有 ${unresolvedFacts} 条事实未验证或缺少来源，不能通过审核。`"
          />
          <el-table :data="current.script.facts" size="small" class="facts">
            <el-table-column label="事实陈述" min-width="210">
              <template #default="{row}">
                <el-input v-model="row.claim" />
              </template>
            </el-table-column>
            <el-table-column label="来源 URL" min-width="240">
              <template #default="{row}">
                <div class="source-input">
                  <el-input v-model="row.source_url" placeholder="https://" />
                  <a v-if="row.source_url" :href="row.source_url" target="_blank" rel="noopener noreferrer">打开</a>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="已核验" width="90">
              <template #default="{row}">
                <el-checkbox v-model="row.verified" />
              </template>
            </el-table-column>
            <el-table-column label="备注" min-width="140">
              <template #default="{row}">
                <el-input v-model="row.notes" />
              </template>
            </el-table-column>
            <el-table-column width="56">
              <template #default="{$index}">
                <el-button link type="danger" @click="current!.script.facts.splice($index,1)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="镜头与素材" name="scenes">
          <div class="scene-layout">
            <aside class="scene-list">
              <el-button type="primary" plain size="small" @click="addScene">添加镜头</el-button>
              <button
                v-for="(scene,index) in current.scenes"
                :key="scene.id"
                :class="{active:activeScene===index}"
                @click="activeScene=index"
              >
                {{ String(index + 1).padStart(2,'0') }}
                <span>{{ scene.caption || scene.narration || '未命名镜头' }}</span>
              </button>
            </aside>
            <div v-if="current.scenes[activeScene]" class="scene-editor">
              <div class="section-title">
                <h3>镜头 {{ activeScene + 1 }}</h3>
                <div>
                  <el-button link type="primary" :disabled="generating || saving" @click="generate('images',current.scenes[activeScene].id)">重绘本镜头配图</el-button>
                  <el-button :disabled="activeScene===0" link @click="moveScene(activeScene,-1)">
                    <el-icon>
                      <ArrowUpBold />
                    </el-icon>
                    上移
                  </el-button>
                  <el-button :disabled="activeScene===current.scenes.length-1" link @click="moveScene(activeScene,1)">
                    <el-icon>
                      <ArrowDownBold />
                    </el-icon>
                    下移
                  </el-button>
                  <el-button link type="danger" @click="removeScene(activeScene)">删除镜头</el-button>
                </div>
              </div>
              <el-form label-position="top">
                <el-form-item label="旁白">
                  <el-input v-model="current.scenes[activeScene].narration" type="textarea" :rows="3" />
                </el-form-item>
                <el-form-item label="画面标题（本地排版）">
                  <el-input v-model="current.scenes[activeScene].caption" type="textarea" :rows="2" />
                </el-form-item>
                <el-form-item label="镜头类型">
                  <el-select v-model="current.scenes[activeScene].scene_type">
                    <el-option v-for="value in supportedSceneTypes" :key="value" :value="value" :label="{image:'图片镜头',text_card:'文字卡片',stat_card:'数据卡片',quote_card:'引用卡片'}[value]" />
                  </el-select>
                  <p v-if="current.scenes[activeScene].scene_type!=='image'" class="form-help">此类型由本地卡片生成，不调用图片模型；图片提示词与上传图片素材不适用。</p>
                </el-form-item>
                <div v-if="current.scenes[activeScene].scene_type!=='image'" class="card-fields">
                  <el-form-item label="卡片标题"><el-input v-model="current.scenes[activeScene].visual_data!.headline" /></el-form-item>
                  <el-form-item :label="current.scenes[activeScene].scene_type==='stat_card'?'数值（需关联已核验事实）':'正文（引用卡片需关联已核验事实）'"><el-input v-if="current.scenes[activeScene].scene_type==='stat_card'" v-model="current.scenes[activeScene].visual_data!.value" /><el-input v-else v-model="current.scenes[activeScene].visual_data!.body" type="textarea" :rows="2" /></el-form-item>
                  <el-form-item label="事实来源 URL"><el-input v-model="current.scenes[activeScene].visual_data!.source_url" placeholder="https://" /></el-form-item>
                  <el-form-item label="强调文字"><el-input v-model="current.scenes[activeScene].visual_data!.emphasis" /></el-form-item>
                </div>
                <el-form-item label="主视觉镜头"><el-checkbox v-model="current.scenes[activeScene].hero_moment">这是视频的关键主视觉时刻</el-checkbox></el-form-item>
                <el-form-item label="镜头意图"><el-input v-model="current.scenes[activeScene].shot_intent" placeholder="例如：用数据对比突出变化" /></el-form-item>
                <el-form-item label="黄色关键词">
                  <el-input-tag v-model="current.scenes[activeScene].highlight_terms" :max="8" placeholder="输入关键词并回车" />
                  <p class="form-help">建议每镜头 1～3 个，需原样出现在旁白或画面标题中；其余字幕保持白色。</p>
                </el-form-item>
                <el-form-item v-if="current.scenes[activeScene].scene_type==='image'" label="画面提示词">
                  <el-input v-model="current.scenes[activeScene].image_prompt" type="textarea" :rows="3" />
                </el-form-item>
                <el-form-item v-if="current.scenes[activeScene].scene_type==='image'" label="画面素材">
                  <el-select
                    v-model="current.scenes[activeScene].image_asset_id"
                    clearable
                    placeholder="选择已上传图片"
                    @clear="clearImageAsset"
                  >
                    <el-option v-for="asset in images" :key="asset.id" :value="asset.id" :label="asset.filename" />
                  </el-select>
                </el-form-item>
                <el-form-item label="音频素材">
                  <el-select
                    v-model="current.scenes[activeScene].audio_asset_ids"
                    multiple
                    clearable
                    placeholder="选择已上传音频"
                  >
                    <el-option v-for="asset in audios" :key="asset.id" :value="asset.id" :label="asset.filename" />
                  </el-select>
                </el-form-item>
                <div class="form-grid motion-controls">
                  <el-form-item label="镜头动画">
                    <el-select v-model="current.scenes[activeScene].motion">
                      <el-option value="auto" label="自动" />
                      <el-option value="none" label="保持静止" />
                      <el-option value="push_in" label="缓慢推进" />
                      <el-option value="pull_out" label="缓慢拉远" />
                      <el-option value="pan_left" label="向左平移" />
                      <el-option value="pan_right" label="向右平移" />
                    </el-select>
                  </el-form-item>
                  <el-form-item label="镜头转场">
                    <el-select v-model="current.scenes[activeScene].transition">
                      <el-option value="fade" label="淡入淡出" />
                      <el-option value="cut" label="直接切换" />
                    </el-select>
                  </el-form-item>
                </div>
              </el-form>
            </div>
          </div>
          <div class="asset-strip">
            <div class="section-title">
              <h3>已上传素材</h3>
              <el-upload
                :show-file-list="false"
                :auto-upload="false"
                :on-change="upload"
                :disabled="uploadLoading"
              >
                <el-button size="small" :loading="uploadLoading">上传图片或音频</el-button>
              </el-upload>
            </div>
            <el-empty v-if="!assets.length" description="暂无素材" :image-size="45" />
            <div v-else class="asset-list">
              <article v-for="asset in assets" :key="asset.id" class="asset-card">
                <img v-if="asset.kind==='image'" :src="asset.url" :alt="asset.filename" />
                <audio v-else-if="asset.kind==='audio'" :src="asset.url" controls preload="metadata" />
                <a :href="asset.url" target="_blank" rel="noopener noreferrer">
                  <b>{{ asset.filename }}</b>
                </a>
                <small>{{ asset.kind }} · {{ formatBytes(asset.size) }}</small>
              </article>
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane label="平台文案" name="platforms">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="发布正文按“描述 + #话题”合并计数。生成内容必须遵守法律及抖音、小红书社区规范，保留AI辅助制作标识，并在发布前人工复核。"
          />
          <el-form label-position="top">
            <el-form-item label="内容模板">
              <el-select v-model="current.template_id" clearable placeholder="选择模板">
                <el-option v-for="template in templates" :key="template.id" :value="template.id" :label="template.name" />
              </el-select>
            </el-form-item>
          </el-form>
          <el-tabs type="border-card">
            <el-tab-pane label="抖音">
              <el-form label-position="top">
                <el-form-item label="标题">
                  <el-input v-model="current.script.platforms.douyin.title" :maxlength="platformTextRules.douyin.titleUnits" />
                  <p class="form-help" :class="{'limit-error':platformTitleUnits(current.script.platforms.douyin.title)>platformTextRules.douyin.titleUnits}">
                    {{ platformTitleUnits(current.script.platforms.douyin.title) }} / {{ platformTextRules.douyin.titleUnits }} 计数单位；中文或全角字符2单位，半角字符1单位（最多30个中文字符）。
                  </p>
                </el-form-item>
                <el-form-item label="描述">
                  <el-input v-model="current.script.platforms.douyin.description" type="textarea" :rows="6" maxlength="1000" />
                  <p class="form-help" :class="{'limit-error':Array.from(platformDescription(current.script.platforms.douyin)).length>platformTextRules.douyin.descriptionChars}">
                    描述与话题合计 {{ Array.from(platformDescription(current.script.platforms.douyin)).length }} / {{ platformTextRules.douyin.descriptionChars }} 字符；描述需明确包含“AI辅助制作”。
                  </p>
                </el-form-item>
                <el-form-item label="标签（每行一个）">
                  <el-input
                    :model-value="current.script.platforms.douyin.tags.join('\n')"
                    type="textarea"
                    :rows="4"
                    @update:model-value="(value:string)=>current!.script.platforms.douyin.tags=value.split('\n').filter(Boolean)"
                  />
                </el-form-item>
              </el-form>
            </el-tab-pane>
            <el-tab-pane label="小红书">
              <el-form label-position="top">
                <el-form-item label="标题">
                  <el-input v-model="current.script.platforms.xiaohongshu.title" :maxlength="platformTextRules.xiaohongshu.titleUnits" />
                  <p class="form-help" :class="{'limit-error':platformTitleUnits(current.script.platforms.xiaohongshu.title)>platformTextRules.xiaohongshu.titleUnits}">
                    {{ platformTitleUnits(current.script.platforms.xiaohongshu.title) }} / {{ platformTextRules.xiaohongshu.titleUnits }} 计数单位；中文或全角字符2单位，半角字符1单位（最多20个中文字符）。
                  </p>
                </el-form-item>
                <el-form-item label="描述">
                  <el-input v-model="current.script.platforms.xiaohongshu.description" type="textarea" :rows="6" maxlength="1000" />
                  <p class="form-help" :class="{'limit-error':Array.from(platformDescription(current.script.platforms.xiaohongshu)).length>platformTextRules.xiaohongshu.descriptionChars}">
                    描述与话题合计 {{ Array.from(platformDescription(current.script.platforms.xiaohongshu)).length }} / {{ platformTextRules.xiaohongshu.descriptionChars }} 字符；描述需明确包含“AI辅助制作”。
                  </p>
                </el-form-item>
                <el-form-item label="标签（每行一个）">
                  <el-input
                    :model-value="current.script.platforms.xiaohongshu.tags.join('\n')"
                    type="textarea"
                    :rows="4"
                    @update:model-value="(value:string)=>current!.script.platforms.xiaohongshu.tags=value.split('\n').filter(Boolean)"
                  />
                </el-form-item>
              </el-form>
            </el-tab-pane>
          </el-tabs>
        </el-tab-pane>
        <el-tab-pane label="预览与审核" name="review">
          <div class="preview-layout">
            <div class="phone">
              <video v-if="current.video_asset_id" :src="`/api/v1/assets/${current.video_asset_id}/file`" :poster="current.cover_asset_id ? `/api/v1/assets/${current.cover_asset_id}/file` : undefined" controls playsinline />
              <div v-else class="video-empty">
                <el-icon>
                  <VideoCamera />
                </el-icon>
                <p>尚无可预览视频</p>
                <small>执行渲染任务后，当前视频会自动显示在这里。</small>
              </div>
            </div>
            <div class="review-panel">
              <h3>审核清单</h3>
              <p>
                <el-icon :class="{good:!!current.video_asset_id && current.media_version===current.version}">
                  <CircleCheckFilled />
                </el-icon>
                当前版本已渲染视频
                <small v-if="current.video_asset_id && current.media_version!==current.version">
                  （媒体版本 v
                  {{current.media_version ?? '—'}}
                  ，当前为 v
                  {{current.version}}
                  ）
                </small>
              </p>
              <p>
                <el-icon :class="{good:current.script.facts.length>0 && !unresolvedFacts}">
                  <CircleCheckFilled />
                </el-icon>
                全部事实已核验且有来源
              </p>
              <p>
                <el-icon :class="{good:!platformIssues.length}">
                  <CircleCheckFilled />
                </el-icon>
                两平台标题、描述与AI标识符合发布规则
              </p>
              <section class="quality-summary">
                <div class="section-title"><h3>Render Quality Review</h3><el-button size="small" :loading="qualityChecking" :disabled="!current.video_asset_id" @click="checkQuality">重新检查</el-button></div>
                <el-alert v-if="!latestQualityReview" type="warning" :closable="false" title="当前视频尚无匹配的质量报告，不能通过审核。" />
                <template v-else><el-alert :type="latestQualityReview.status==='PASS'?'success':latestQualityReview.status==='FAIL'?'error':'warning'" :closable="false" :title="latestQualityReview.status==='PASS'?'质量检查通过。仍需完成人工事实、画面与合规审核。':`质量检查结果：${latestQualityReview.status}，请根据问题修改后重新渲染或复检。`" /><ul v-if="latestQualityReview.report.issues?.length" class="quality-issues"><li v-for="(issue,index) in latestQualityReview.report.issues" :key="index">{{issue.message || issue.code || '未命名质量问题'}}</li></ul><p v-if="latestQualityReview.report.warnings?.length" class="form-help">警告：{{latestQualityReview.report.warnings.map(item=>item.message || item.code || '未命名警告').join('；')}}</p><p class="form-help">建议动作：{{latestQualityReview.report.recommended_action || '需人工复核'}}</p></template>
              </section>
              <el-alert v-if="platformIssues.length" type="warning" :closable="false" :title="platformIssues.join('；')" />
              <el-alert
                :type="readyToApprove?'success':'warning'"
                :closable="false"
                :title="readyToApprove?'满足提交审核条件，服务端会再次校验当前版本与质量报告。':'请先生成当前版本视频，取得 PASS 质量报告，核验全部事实及来源，并修正平台文案。'"
              />
              <div class="review-actions">
                <el-button type="danger" plain :loading="rejecting" @click="reject">驳回修改</el-button>
                <el-button type="success" :loading="approving" :disabled="!readyToApprove || saving" @click="approve">通过审核</el-button>
              </div>
              <a class="export-link" :href="api.exportUrl(current.id)">下载当前版本导出包（ZIP）</a>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </template>
  </section>
</div>
<el-dialog v-model="approvalDialog" title="确认通过" width="min(620px, calc(100% - 32px))">
  <el-alert type="warning" :closable="false" title="请逐项完成合规确认。系统不以关键词扫描替代你的版权或平台规则判断。" />
  <el-alert v-if="approvalAutomatic" type="error" :closable="false" title="当前已开启审核后自动发布：通过后将自动上传并发布到已绑定账号。" />
  <el-alert v-else type="info" :closable="false" title="当前为手动准备模式：通过审核不会点击平台最终发布按钮。" />
  <el-form label-position="top" class="compliance-form"><el-form-item label="结构化合规清单"><el-checkbox v-model="complianceChecks.source_rights_confirmed">已确认来源与素材使用权</el-checkbox><el-checkbox v-model="complianceChecks.no_unauthorized_material">不含未获授权材料</el-checkbox><el-checkbox v-model="complianceChecks.claims_not_exaggerated">事实主张未夸大</el-checkbox><el-checkbox v-model="complianceChecks.no_offplatform_diversion">不含站外导流</el-checkbox><el-checkbox v-model="complianceChecks.no_unverifiable_guarantees">不含不可验证承诺</el-checkbox><el-checkbox v-model="complianceChecks.ai_disclosure_confirmed">已确认 AI 辅助制作标识</el-checkbox><el-checkbox v-model="complianceChecks.platform_rules_checked">已按当前平台规则人工核对</el-checkbox><el-checkbox v-model="complianceChecks.visuals_manually_reviewed">已人工复核画面、文案与素材</el-checkbox></el-form-item><el-form-item label="审核说明（可选）"><el-input v-model="approvalNotes" type="textarea" :rows="3" placeholder="审核说明" /></el-form-item></el-form>
  <template #footer><el-button @click="approvalDialog=false">取消</el-button><el-button type="success" :loading="approving" :disabled="!allComplianceChecked" @click="submitApproval">已核对，通过</el-button></template>
</el-dialog>
</template>

<style scoped>
.form-help.limit-error { color: #d04444; }
.production-brief{margin-top:14px}.brief-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.brief-card{border:1px solid #e3e7ef;border-radius:8px;padding:13px;display:grid;gap:8px}.brief-card.selected{border-color:#46ad71;background:#f3fbf6}.brief-card p{margin:0;color:#596579;line-height:1.55}.brief-card small{color:#7d8898;line-height:1.6}.quality-summary{margin:16px 0}.quality-summary h3{margin:0}.quality-issues{padding-left:20px;color:#c45656;line-height:1.6}.compliance-form .el-checkbox{display:flex;margin:0 0 10px;white-space:normal}.card-fields{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}@media(max-width:800px){.brief-grid{grid-template-columns:1fr}.card-fields{grid-template-columns:1fr}}
</style>
