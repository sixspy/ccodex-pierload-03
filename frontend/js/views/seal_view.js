export const sealViewTemplate = `
<section class="panel">
  <h2>闸代快照</h2>
  <p class="hint">封闸升代并写入清单摘要，供提单册对账引用。旁路开放禁封闸：开放挂起票不进封闸口，拒写留「禁封偏航拒写」命书；影子快照草稿不计已封。</p>
  <div class="form-row">
    <div><label>过磅票（仅已裁定/已放行）</label>
      <select v-model.number="sealForm.ticket_id">
        <option v-for="t in sealableTickets" :key="t.id" :value="t.id">{{ t.ticket_code }} · {{ t.status }}</option>
      </select>
    </div>
    <div><label>清单(可空)</label><input v-model="sealForm.manifest"/></div>
  </div>
  <p v-if="openBypassTickets.length" class="action-err">
    旁路开放禁封：{{ openBypassTickets.map(t => t.ticket_code).join('、') }} 挂起未闭环，封闸零增并拒写回滚。
  </p>
  <div class="actions">
    <button @click="createSeal" :disabled="!sealableTickets.length">写入正式快照</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <table class="data-table">
    <thead><tr><th>快照号</th><th>票号</th><th>闸代</th><th>清单摘要</th><th>状态</th></tr></thead>
    <tbody>
      <tr v-for="s in seals" :key="s.id">
        <td>{{ s.seal_ref }}</td>
        <td>{{ s.ticket_code }}</td>
        <td>{{ s.ticket_generation }}</td>
        <td class="mono">{{ shortText(s.manifest) }}</td>
        <td><span class="badge" :class="sealBadgeClass(s.status)">{{ s.status }}</span></td>
      </tr>
    </tbody>
  </table>
</section>
`;