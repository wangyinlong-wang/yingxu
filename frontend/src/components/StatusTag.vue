<script setup lang="ts">
const props = defineProps<{
    status: string;
}>();
const map: Record<string, [
    string,
    string
]> = { DRAFT: ['info', '草稿'], GENERATING: ['warning', '生成中'], REVIEW_REQUIRED: ['warning', '待复核'], APPROVED: ['success', '已通过'], REJECTED: ['danger', '已驳回'], QUEUED: ['info', '排队中'], RUNNING: ['primary', '执行中'], WAITING_USER: ['warning', '等待人工'], SUCCEEDED: ['success', '已完成'], FAILED: ['danger', '失败'], CANCELED: ['info', '已取消'], PREPARING: ['primary', '准备中'], VERIFYING: ['primary', '核验中'], PUBLISHED: ['success', '已发布'], NEEDS_ATTENTION: ['danger', '需处理'] };
</script><template>
<el-tag size="small" :type="map[props.status]?.[0] as any" effect="light">
  {{ map[props.status]?.[1] || props.status }}
</el-tag>
</template>
