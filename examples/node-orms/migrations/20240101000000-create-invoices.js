'use strict';

module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable('Invoices', {
      id: { type: Sequelize.INTEGER, primaryKey: true, autoIncrement: true },
      userId: { type: Sequelize.INTEGER, references: { model: 'users', key: 'id' } },
      paid: { type: Sequelize.BOOLEAN, defaultValue: false },
      number: { type: Sequelize.STRING, unique: true },
    });
    await queryInterface.addIndex('Invoices', ['number']);
    await queryInterface.addIndex('Invoices', { fields: ['userId', 'paid'] });
    await queryInterface.addIndex('Invoices', ['userId']);
  },

  async down(queryInterface) {
    await queryInterface.dropTable('Invoices');
  },
};
