// Preserve the official sections; publish the project role as system context.
export function hospitalContext(sections,instructions){
  return {sections:[...sections.filter(s=>s.id!=='hospital-delivery-role'),{id:'hospital-delivery-role',order:100,content:instructions}]};
}
