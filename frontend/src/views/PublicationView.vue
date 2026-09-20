<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api';
import { actionKey, formatDate } from '../lib';
import type { Account, Content, Publication } from '../types';
import PageIntro from '../components/PageIntro.vue';
import StatePanel from '../components/StatePanel.vue';
import StatusTag from '../components/StatusTag.vue';
const publications = ref<Publication[]>([]);
const accounts = ref<Account[]>([]);
const devices = ref<any[]>([]);
const contents = ref<Content[]>([]);
const automaticPosting = ref(false);
const inspections = ref<Record<string, any>>({});
const loading = ref(false), error = ref('');
const accountDialog = ref(false), publishDialog = ref(false), confirmDialog = ref(false), detailDrawer = ref(false);
const savingAccount = ref(false), preparing = ref(false), confirming = ref(false);
const cancelling = ref<string | null>(null), resuming = ref<string | null>(null), inspecting = ref<string | null>(null), detecting = ref<string | null>(null);
const selectedPublication = ref<Publication | null>(null), editingAccount = ref<Account | null>(null), confirmTarget = ref<Publication | null>(null);
const timer = ref<number>();
const accountForm = ref<any>({ platform: 'douyin', display_name: '', account_identifier: '', device_serial: '' });
const publishForm = ref({ content_id: '', account_id: '' });
const confirmForm = ref({ work_url: '', work_id: '', notes: '', confirmed: false });
const approvedContents = computed(() => contents.value.filter(content => content.status === 'APPROVED'));
const chosenContent = computed(() => contents.value.find(content => content.id === publishForm.value.content_id));
const inspectionKey = (serial: string, platform: string) => `${serial}:${platform}`;
const evidenceValue = (publication: Publication, key: string) => typeof publication.evidence === 'object' && publication.evidence ? (publication.evidence as any)[key] : undefined;
const handoffText = (publication: Publication) => publication.status === 'WAITING_USER' ? '请在手机编辑器核对账号、视频和文案。发布后打开该作品详情，工作台可自动识别链接。' : publication.status === 'NEEDS_ATTENTION' ? '设备现场状态需要处理；连接绑定手机后继续准备，或打开已发作品让工作台识别。' : publication.status === 'VERIFYING' ? '最终发布已提交且不会重复点击；请在手机打开该作品详情，由工作台识别并完成核验。' : '当前无需人工接管。';
const accountFor = (publication: Publication) => accounts.value.find(account => account.id === publication.account_id);
const hasConnectedDevice = (publication: Publication) => {
    const account = accountFor(publication);
    return !!account && devices.value.some(device => device.serial === account.device_serial && device.state === 'device');
};
async function load() {
    loading.value = true;
    error.value = '';
    try {
        const [publicationResponse, accountResponse, deviceResponse, contentResponse, settingsResponse] = await Promise.all([api.publications(), api.accounts(), api.devices(), api.contents('', ''), api.settings()]);
        publications.value = publicationResponse.items;
        accounts.value = accountResponse.items;
        devices.value = deviceResponse.items;
        contents.value = contentResponse.items;
        automaticPosting.value = !!settingsResponse.publishing?.auto_publish_after_approval;
    }
    catch (exception: any) {
        error.value = exception.message;
    }
    finally {
        loading.value = false;
    }
}
async function pollPublications() {
    if (confirmDialog.value || publishDialog.value || accountDialog.value || confirming.value || preparing.value || cancelling.value || resuming.value)
        return;
    try {
        publications.value = (await api.publications()).items;
        if (selectedPublication.value)
            selectedPublication.value = publications.value.find(item => item.id === selectedPublication.value?.id) || null;
    }
    catch { /* retain usable state during transient polling failures */ }
}
function openAccount(account?: Account) {
    editingAccount.value = account || null;
    accountForm.value = account ? { ...account } : { platform: 'douyin', display_name: '', account_identifier: '', device_serial: '' };
    accountDialog.value = true;
}
async function saveAccount() {
    if (savingAccount.value)
        return;
    savingAccount.value = true;
    try {
        if (editingAccount.value)
            await api.patchAccount(editingAccount.value.id, accountForm.value);
        else
            await api.createAccount(accountForm.value);
        accountDialog.value = false;
        await load();
        ElMessage.success(editingAccount.value ? '账号已更新' : '账号已保存');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        savingAccount.value = false;
    }
}
async function inspect(serial: string, platform: 'douyin' | 'xiaohongshu') {
    const key = inspectionKey(serial, platform);
    if (inspecting.value)
        return;
    inspecting.value = key;
    try {
        const result: any = await api.inspect(serial, platform, actionKey());
        inspections.value = { ...inspections.value, [key]: result };
        ElMessage[result.ready ? 'success' : 'warning'](result.ready ? `${platform === 'douyin' ? '抖音' : '小红书'}设备已就绪` : result.warnings?.join('；') || '设备尚未就绪');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        inspecting.value = null;
    }
}
async function prepare() {
    if (!chosenContent.value || preparing.value)
        return;
    preparing.value = true;
    try {
        await api.publish({ content_id: chosenContent.value.id, content_version: chosenContent.value.version, account_id: publishForm.value.account_id }, actionKey());
        publishDialog.value = false;
        await load();
        ElMessage.success('已创建发布准备任务。请在手机端检查最终内容，系统不会代为点击发布。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        preparing.value = false;
    }
}
function openConfirm(publication: Publication) {
    confirmTarget.value = publication;
    confirmForm.value = { work_url: publication.work_url || '', work_id: publication.work_id || '', notes: '', confirmed: false };
    confirmDialog.value = true;
}
async function confirmPublication() {
    if (!confirmTarget.value || confirming.value)
        return;
    confirming.value = true;
    try {
        await api.confirmPublication(confirmTarget.value.id, { work_url: confirmForm.value.work_url, work_id: confirmForm.value.work_id || undefined, confirmed: true, notes: confirmForm.value.notes || undefined }, actionKey());
        confirmDialog.value = false;
        await load();
        ElMessage.success('已记录人工确认的实际发布作品');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        confirming.value = false;
    }
}
async function detectPublication(publication: Publication) {
    if (detecting.value)
        return;
    try {
        await ElMessageBox.confirm('请先在绑定手机上打开刚发布的这条作品详情页。工作台会核对已审核标题并读取分享链接，不会再次点击发布。', '从手机识别已发作品', { confirmButtonText: '开始识别', cancelButtonText: '返回', type: 'info' });
    }
    catch {
        return;
    }
    detecting.value = publication.id;
    try {
        await api.detectPublication(publication.id, actionKey());
        await load();
        ElMessage.success('已从绑定手机识别作品链接并完成发布记录');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        detecting.value = null;
    }
}
async function cancel(publication: Publication) {
    let confirmation: any = {};
    try {
        if (evidenceValue(publication, 'submit_started_at')) {
            await ElMessageBox.prompt('请先在平台核实没有发布这条作品，再输入“未发布”解除防重。已经发布的作品请回填链接。', '核验发布结果', { inputPattern: /^未发布$/, inputErrorMessage: '需核实平台结果并输入“未发布”', confirmButtonText: '确认未发布并取消', cancelButtonText: '返回', type: 'warning' });
            confirmation = { confirmed_not_published: true, notes: '用户在平台核验后确认未发布' };
        } else {
            await ElMessageBox.confirm('取消只会停止本地准备流程，不会清理手机端已打开的编辑器或撤回已发作品。请先自行核验手机现场状态。', '取消本地流程', { confirmButtonText: '我已核验，停止本地流程', cancelButtonText: '返回', type: 'warning' });
        }
    }
    catch {
        return;
    }
    cancelling.value = publication.id;
    try {
        await api.cancelPublication(publication.id, confirmation, actionKey());
        await load();
        ElMessage.success('本地发布流程已取消；手机现场状态未被修改。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        cancelling.value = null;
    }
}
async function resume(publication: Publication) {
    if (resuming.value)
        return;
    resuming.value = publication.id;
    try {
        await api.resumePublication(publication.id, actionKey());
        await load();
        ElMessage.success('已继续准备，请在手机端按当前页面核验。');
    }
    catch (exception: any) {
        ElMessage.error(exception.message);
    }
    finally {
        resuming.value = null;
    }
}
function openDetail(publication: Publication) { selectedPublication.value = publication; detailDrawer.value = true; }
onMounted(() => { load(); timer.value = window.setInterval(pollPublications, 5000); });
onBeforeUnmount(() => { if (timer.value)
    clearInterval(timer.value); });
</script>

<template>
<PageIntro title="发布与设备" :description="automaticPosting?'审核通过后系统会自动上传、点击一次最终发布并读取作品链接；异常结果不会重复提交。':'手动准备会停在手机编辑器；发布后可由工作台从当前作品页自动识别链接。'">
  <el-button @click="load">刷新</el-button>
  <el-button @click="openAccount()">账号设置</el-button>
  <el-button type="primary" :disabled="!approvedContents.length || !accounts.length" @click="publishDialog=true">
    开始准备
  </el-button>
</PageIntro>
<StatePanel :loading :error @retry="load">
  <el-alert
    v-if="automaticPosting"
    type="success"
    :closable="false"
    title="审核后自动发布已开启：新审核内容会自动排队，成功后无需复制作品链接。"
  />
  <el-alert
    v-if="accounts.length && !devices.length"
    type="warning"
    :closable="false"
    title="已配置发布账号，但当前未发现 ADB 手机；自动发布与手机识别会暂停，重新连接并完成 USB 调试授权后可继续。"
  />
  <section class="device-strip">
    <el-card shadow="never">
      <template #header>已配置账号</template>
      <el-empty v-if="!accounts.length" description="尚未配置账号" :image-size="45" />
      <div
        v-else
        v-for="account in accounts"
        :key="account.id"
        class="line-item"
      >
        <span>
          <b>{{account.display_name}}</b>
          <small>{{account.platform}} · {{account.account_identifier}}</small>
        </span>
        <div>
          <code>{{account.device_serial}}</code>
          <el-button link type="primary" @click="openAccount(account)">编辑</el-button>
        </div>
      </div>
    </el-card>
    <el-card shadow="never">
      <template #header>已发现设备</template>
      <el-empty v-if="!devices.length" description="未发现 ADB 设备" :image-size="45" />
      <div
        v-else
        v-for="device in devices"
        :key="device.serial"
        class="device-line"
      >
        <div class="line-item">
          <span>
            <b>{{device.serial}}</b>
            <small>{{device.model || '未知设备'}} · {{device.state}}</small>
          </span>
          <div>
            <el-button
              link
              type="primary"
              :loading="inspecting===inspectionKey(device.serial,'douyin')"
              @click="inspect(device.serial,'douyin')"
            >
              检查抖音
            </el-button>
            <el-button
              link
              type="primary"
              :loading="inspecting===inspectionKey(device.serial,'xiaohongshu')"
              @click="inspect(device.serial,'xiaohongshu')"
            >
              检查小红书
            </el-button>
          </div>
        </div>
        <div v-for="platform in ['douyin','xiaohongshu']" :key="platform">
          <div v-if="inspections[inspectionKey(device.serial,platform)]" class="inspection-result">
            <b>{{platform==='douyin'?'抖音':'小红书'}}：{{inspections[inspectionKey(device.serial,platform)].ready?'已就绪':'需处理'}}</b>
            <span>
              版本
              {{inspections[inspectionKey(device.serial,platform)].app_version || '—'}}
              · 账号
              {{inspections[inspectionKey(device.serial,platform)].account_identifier || '—'}}
            </span>
            <small v-if="inspections[inspectionKey(device.serial,platform)].warnings?.length">
              {{inspections[inspectionKey(device.serial,platform)].warnings.join('；')}}
            </small>
          </div>
        </div>
      </div>
    </el-card>
  </section>
  <section>
    <h3>发布记录</h3>
    <el-empty v-if="!publications.length" description="暂无发布记录" />
    <el-table
      v-else
      :data="publications"
      class="data-table"
      @row-click="openDetail"
    >
      <el-table-column prop="content_title" label="内容" min-width="190" />
      <el-table-column prop="platform" label="平台" width="110" />
      <el-table-column label="状态" width="130">
        <template #default="{row}">
          <StatusTag :status="row.status" />
        </template>
      </el-table-column>
      <el-table-column label="作品链接" min-width="180">
        <template #default="{row}">
          <a v-if="row.work_url" :href="row.work_url" target="_blank" rel="noopener noreferrer" @click.stop>查看作品</a>
          <span v-else class="muted">尚未确认</span>
        </template>
      </el-table-column>
      <el-table-column label="发布方式" width="110">
        <template #default="{row}">{{row.auto_publish?'审核后自动':'手机确认'}}</template>
      </el-table-column>
      <el-table-column label="更新时间" width="165">
        <template #default="{row}">{{formatDate(row.updated_at)}}</template>
      </el-table-column>
      <el-table-column label="操作" width="330">
        <template #default="{row}">
          <el-button
            v-if="['WAITING_USER','NEEDS_ATTENTION','VERIFYING'].includes(row.status)"
            link
            type="primary"
            :loading="detecting===row.id"
            :disabled="!hasConnectedDevice(row)"
            @click.stop="detectPublication(row)"
          >
            从手机识别
          </el-button>
          <el-button
            v-if="['WAITING_USER','NEEDS_ATTENTION','VERIFYING'].includes(row.status)"
            link
            type="success"
            @click.stop="openConfirm(row)"
          >
            确认已发布
          </el-button>
          <el-button
            v-if="['WAITING_USER','NEEDS_ATTENTION'].includes(row.status) && !evidenceValue(row,'submit_started_at')"
            link
            type="primary"
            :loading="resuming===row.id"
            @click.stop="resume(row)"
          >
            继续准备
          </el-button>
          <el-button
            v-if="!['PUBLISHED','CANCELED'].includes(row.status)"
            link
            type="danger"
            :loading="cancelling===row.id"
            @click.stop="cancel(row)"
          >
            取消
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</StatePanel>
<el-dialog v-model="accountDialog" :title="editingAccount?'编辑发布账号':'配置发布账号'" width="480px">
  <el-alert type="info" :closable="false" title="仅保存平台、账号标识与已连接设备；系统不会保存登录密码或密钥。"/>
  <el-form label-position="top">
    <el-form-item label="平台">
      <el-radio-group v-model="accountForm.platform" :disabled="!!editingAccount">
        <el-radio value="douyin">抖音</el-radio>
        <el-radio value="xiaohongshu">小红书</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item label="显示名称">
      <el-input v-model="accountForm.display_name"/>
    </el-form-item>
    <el-form-item label="账号标识">
      <el-input v-model="accountForm.account_identifier"/>
    </el-form-item>
    <el-form-item label="设备序列号">
      <el-input v-model="accountForm.device_serial"/>
    </el-form-item>
  </el-form>
  <template #footer>
    <el-button :disabled="savingAccount" @click="accountDialog=false">取消</el-button>
    <el-button
      type="primary"
      :loading="savingAccount"
      :disabled="!accountForm.display_name||!accountForm.account_identifier||!accountForm.device_serial"
      @click="saveAccount"
    >
      保存
    </el-button>
  </template>
</el-dialog>
<el-dialog v-model="publishDialog" title="准备手机端发布" width="520px">
  <el-alert type="warning" :closable="false" title="这是手动补充准备入口：系统会停在编辑器，不点击最终发布；发布后可在列表中从手机自动识别链接。"/>
  <el-form label-position="top">
    <el-form-item label="已审核内容">
      <el-select v-model="publishForm.content_id">
        <el-option
          v-for="content in approvedContents"
          :key="content.id"
          :value="content.id"
          :label="`${content.title}（v${content.version}）`"
        />
      </el-select>
    </el-form-item>
    <el-form-item label="发布账号">
      <el-select v-model="publishForm.account_id">
        <el-option
          v-for="account in accounts"
          :key="account.id"
          :value="account.id"
          :label="`${account.platform} · ${account.display_name}`"
        />
      </el-select>
    </el-form-item>
  </el-form>
  <template #footer>
    <el-button :disabled="preparing" @click="publishDialog=false">取消</el-button>
    <el-button
      type="primary"
      :loading="preparing"
      :disabled="!publishForm.content_id||!publishForm.account_id"
      @click="prepare"
    >
      开始准备
    </el-button>
  </template>
</el-dialog>
<el-dialog v-model="confirmDialog" title="确认手机端已发布" width="540px">
  <el-alert type="warning" :closable="false" title="仅在你已于手机端实际完成最终发布后确认。短链接无法解析时，请补充作品 ID。"/>
  <el-form label-position="top">
    <el-form-item label="作品 URL">
      <el-input v-model="confirmForm.work_url" placeholder="https://…"/>
    </el-form-item>
    <el-form-item label="作品 ID（短链接无法解析时补充，可选）">
      <el-input v-model="confirmForm.work_id"/>
    </el-form-item>
    <el-form-item label="说明（可选）">
      <el-input v-model="confirmForm.notes" type="textarea" :rows="3"/>
    </el-form-item>
    <el-checkbox v-model="confirmForm.confirmed">我确认已在手机端实际发布此作品</el-checkbox>
  </el-form>
  <template #footer>
    <el-button :disabled="confirming" @click="confirmDialog=false">返回</el-button>
    <el-button
      type="success"
      :loading="confirming"
      :disabled="!confirmForm.work_url || !confirmForm.confirmed"
      @click="confirmPublication"
    >
      记录确认
    </el-button>
  </template>
</el-dialog>
<el-drawer v-model="detailDrawer" title="发布详情" size="460px">
  <template v-if="selectedPublication">
    <div class="publication-detail">
      <div>
        <StatusTag :status="selectedPublication.status"/>
        <b>{{selectedPublication.content_title}}</b>
      </div>
      <p>
        <b>当前步骤：</b>
        {{(selectedPublication as any).current_step || evidenceValue(selectedPublication,'current_step') || '等待服务端更新'}}
      </p>
      <p>
        <b>接管指引：</b>
        {{handoffText(selectedPublication)}}
      </p>
      <p v-if="evidenceValue(selectedPublication,'reason')">
        <b>原因：</b>
        {{evidenceValue(selectedPublication,'reason')}}
      </p>
      <p v-if="evidenceValue(selectedPublication,'error_code')">
        <b>错误码：</b>
        {{evidenceValue(selectedPublication,'error_code')}}
      </p>
      <pre v-if="selectedPublication.evidence">{{JSON.stringify(selectedPublication.evidence,null,2)}}</pre>
    </div>
  </template>
</el-drawer>
</template>
