const { Invoice } = require('../models');

async function remindUnpaid() {
  const invoices = await Invoice.findAll({ where: { paid: false } });
  for (const inv of invoices) {
    const user = await inv.getUser();
    await sendMail(user.email);
    await inv.update({ reminded: true });
  }
}

module.exports = { remindUnpaid };
